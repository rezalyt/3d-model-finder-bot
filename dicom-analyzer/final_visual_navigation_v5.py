#!/usr/bin/env python3
"""Visual/navigation package for all unified abdominal screening candidates.

Research/screening only. Generates axial PNGs for every candidate across the
available phases, with center +/- 2 slices, and stores DICOM navigation metadata.
No repeat TotalSegmentator run is performed.
"""
import argparse
import csv
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pydicom
import nibabel as nib
from PIL import Image, ImageDraw
import dicom2nifti

PHASES = ("native", "arterial", "portal", "delayed")
SKIP = ("topogram", "scout", "localizer", "monitor", "premonitor")


def py(v):
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, dict):
        return {k: py(x) for k, x in v.items()}
    if isinstance(v, list):
        return [py(x) for x in v]
    if isinstance(v, tuple):
        return [py(x) for x in v]
    return v


def dump_json(path, data):
    path.write_text(json.dumps(py(data), ensure_ascii=False, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o)), encoding="utf-8")


def inventory(zip_path):
    series = {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            p = Path(info.filename)
            if p.is_absolute() or ".." in p.parts:
                continue
            try:
                with z.open(info) as fh:
                    ds = pydicom.dcmread(fh, stop_before_pixels=True, force=True)
                if getattr(ds, "Modality", "") != "CT" or not hasattr(ds, "Rows"):
                    continue
                uid = str(getattr(ds, "SeriesInstanceUID", "unknown"))
                series.setdefault(uid, []).append({
                    "name": info.filename,
                    "instance": int(getattr(ds, "InstanceNumber", 0) or 0),
                    "description": str(getattr(ds, "SeriesDescription", "")),
                })
            except Exception:
                continue
    for items in series.values():
        items.sort(key=lambda x: (x["instance"], x["name"]))
    return series


def classify_phase(description):
    d = description.lower().replace("_", " ")
    rules = {
        "native": ("non contrast", "non-contrast", "noncontrast", "native", "without contrast"),
        "arterial": ("arterial",),
        "portal": ("portal", "venous", "porto"),
        "delayed": ("delayed",),
    }
    for phase, words in rules.items():
        if any(w in d for w in words):
            return phase
    return "other"


def copy_series(zip_path, items, dst):
    dst.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for n, item in enumerate(items, 1):
            with z.open(item["name"]) as src, open(dst / f"slice_{n:05d}.dcm", "wb") as out:
                shutil.copyfileobj(src, out, 4 * 1024 * 1024)


def reconstruct(zip_path, items, tmp):
    dcm = tmp / "dicom"
    nii = tmp / "nifti"
    copy_series(zip_path, items, dcm)
    nii.mkdir(parents=True, exist_ok=True)
    dicom2nifti.convert_directory(str(dcm), str(nii), compression=True, reorient=True)
    files = sorted(nii.glob("*.nii.gz"))
    if not files:
        raise RuntimeError("DICOM-to-NIfTI conversion produced no volume")
    img = nib.load(str(files[0]))
    return img, img.get_fdata(dtype=np.float32)


def normalize_slice(arr, center=40.0, width=400.0):
    lo = center - width / 2.0
    hi = center + width / 2.0
    x = np.clip((arr - lo) / max(hi - lo, 1.0), 0.0, 1.0)
    return (x * 255.0).astype(np.uint8)


def crop(arr, cy, cx, size=256):
    h, w = arr.shape
    if h <= size or w <= size:
        return arr, 0, 0
    half = size // 2
    y0 = max(0, min(h - size, int(round(cy)) - half))
    x0 = max(0, min(w - size, int(round(cx)) - half))
    return arr[y0:y0 + size, x0:x0 + size], y0, x0


def make_overlay(vol, obs, title, out_path, phase, offset):
    z, y, x = [float(v) for v in obs["centroid_zyx"]]
    zi = max(0, min(vol.shape[0] - 1, int(round(z)) + offset))
    axial = vol[zi, :, :]
    image, y0, x0 = crop(axial, y, x)
    base = Image.fromarray(normalize_slice(image), mode="L").convert("RGB")
    draw = ImageDraw.Draw(base)
    px = int(round(x)) - x0
    py = int(round(y)) - y0
    r = max(6, min(16, int(round(max(image.shape) / 20))))
    draw.ellipse((px - r, py - r, px + r, py + r), outline=(255, 0, 0), width=3)
    draw.line((px - 2 * r, py, px + 2 * r, py), fill=(255, 0, 0), width=1)
    draw.line((px, py - 2 * r, px, py + 2 * r), fill=(255, 0, 0), width=1)
    canvas = Image.new("RGB", (base.width, base.height + 50), (25, 25, 25))
    canvas.paste(base, (0, 50))
    d = ImageDraw.Draw(canvas)
    d.text((8, 7), title, fill=(255, 255, 255))
    d.text((8, 27), f"phase={phase}  slice_offset={offset}  z={zi}  HU={obs.get('mean_hu', 'n/a')}", fill=(220, 220, 220))
    canvas.save(out_path)
    return zi


def load_dicom_navigation(zip_path, items):
    nav = []
    with zipfile.ZipFile(zip_path) as z:
        for idx, item in enumerate(items):
            try:
                with z.open(item["name"]) as fh:
                    ds = pydicom.dcmread(fh, stop_before_pixels=True, force=True)
                iop = getattr(ds, "ImageOrientationPatient", None)
                ipp = getattr(ds, "ImagePositionPatient", None)
                ps = getattr(ds, "PixelSpacing", None)
                nav.append({
                    "index": idx,
                    "file": item["name"],
                    "instance_number": int(getattr(ds, "InstanceNumber", 0) or 0),
                    "sop_instance_uid": str(getattr(ds, "SOPInstanceUID", "")),
                    "series_instance_uid": str(getattr(ds, "SeriesInstanceUID", "")),
                    "image_position_patient": list(ipp) if ipp is not None else None,
                    "image_orientation_patient": list(iop) if iop is not None else None,
                    "pixel_spacing": list(ps) if ps is not None else None,
                    "slice_thickness": float(getattr(ds, "SliceThickness", 0) or 0),
                })
            except Exception:
                continue
    return nav


def nearest_dicom_slice(nav, world_point):
    candidates = [x for x in nav if x.get("image_position_patient")]
    if not candidates:
        return None
    p = np.asarray(world_point, dtype=float)
    best = min(candidates, key=lambda x: float(np.linalg.norm(np.asarray(x["image_position_patient"], dtype=float) - p)))
    return best


def affine_world(img, voxel):
    return (np.asarray(img.affine) @ np.asarray([voxel[2], voxel[1], voxel[0], 1.0]))[:3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip", type=Path)
    ap.add_argument("--input", type=Path, default=Path("dicom-ai-result/final_screening/final_screening.json"))
    ap.add_argument("--out", type=Path, default=Path("dicom-ai-result/final_visual_review_v5"))
    ap.add_argument("--neighbors", type=int, default=2)
    args = ap.parse_args()
    if not args.zip.exists():
        raise SystemExit(f"ZIP not found: {args.zip}")
    if not args.input.exists():
        raise SystemExit(f"Final screening JSON not found: {args.input}")

    report = json.loads(args.input.read_text(encoding="utf-8"))
    candidates = report.get("candidates", [])
    if not candidates:
        raise SystemExit("No candidates found in final_screening.json")

    root = args.out
    root.mkdir(parents=True, exist_ok=True)
    all_series = inventory(args.zip)
    phase_map = {}
    for uid, items in all_series.items():
        if len(items) < 25:
            continue
        desc = items[0]["description"]
        if any(w in desc.lower() for w in SKIP):
            continue
        phase = classify_phase(desc)
        if phase == "other":
            continue
        if phase not in phase_map or len(items) > len(phase_map[phase]["items"]):
            phase_map[phase] = {"uid": uid, "items": items, "description": desc}

    # Process one phase at a time to keep RAM bounded.
    outputs = {str(c.get("candidate_id")): {"candidate": py(c), "phase_outputs": [], "errors": []} for c in candidates}
    for phase in PHASES:
        series = phase_map.get(phase)
        if not series:
            continue
        obs_map = {}
        for c in candidates:
            cid = str(c.get("candidate_id"))
            obs = [o for o in c.get("observations", []) if o.get("phase") == phase]
            if not obs:
                obs = c.get("observations", [])[:1]
            if obs:
                obs_map[cid] = obs[0]

        nav_cache = load_dicom_navigation(args.zip, series["items"])
        with tempfile.TemporaryDirectory(prefix="final_v5_") as td:
            try:
                img, vol = reconstruct(args.zip, series["items"], Path(td))
            except Exception as exc:
                for cid in outputs:
                    outputs[cid]["errors"].append({"phase": phase, "error": str(exc)})
                continue

            for c in candidates:
                cid = str(c.get("candidate_id"))
                obs = obs_map.get(cid)
                if obs is None:
                    continue
                cdir = root / cid / phase
                cdir.mkdir(parents=True, exist_ok=True)
                z = float(obs["centroid_zyx"][0])
                world = affine_world(img, obs["centroid_zyx"])
                nearest = nearest_dicom_slice(nav_cache, world)
                generated = []
                for offset in range(-args.neighbors, args.neighbors + 1):
                    fn = cdir / f"axial_{offset:+d}.png"
                    try:
                        zi = make_overlay(vol, obs, f"{cid} | {c.get('organ')} | {phase}", fn, phase, offset)
                        generated.append({"offset": offset, "nifti_slice_index": zi, "path": str(fn)})
                    except Exception as exc:
                        outputs[cid]["errors"].append({"phase": phase, "offset": offset, "error": str(exc)})
                outputs[cid]["phase_outputs"].append({
                    "phase": phase,
                    "series_description": series["description"],
                    "series_instance_uid": series["uid"],
                    "candidate_observation": py(obs),
                    "nifti_world_coordinate": py(world),
                    "dicom_navigation": py(nearest) if nearest else None,
                    "images": generated,
                })
        print(f"[OK] Visual navigation phase {phase}: {series['description']}")

    final = {"type": "research_screening_visual_navigation_v5", "warning": "Visual/navigation support only; not a medical diagnosis.", "summary": {"input_candidates": len(candidates), "candidates_processed": len(outputs), "images_generated": sum(len(p.get('images', [])) for o in outputs.values() for p in o.get('phase_outputs', []))}, "candidates": list(outputs.values())}
    dump_json(root / "final_visual_review_v5.json", final)
    with (root / "final_visual_review_v5.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["candidate_id", "organ", "level", "score", "phases", "images", "errors"])
        for cid, item in outputs.items():
            c = item["candidate"]
            w.writerow([cid, c.get("organ"), c.get("triage_level"), c.get("triage_score"), ",".join(x["phase"] for x in item["phase_outputs"]), sum(len(x["images"]) for x in item["phase_outputs"]), " | ".join(e["phase"]+":"+e["error"] for e in item["errors"])])
    print(f"Completed visual navigation v5: {len(outputs)} candidates, {final['summary']['images_generated']} images")
    print(f"JSON: {root / 'final_visual_review_v5.json'}")
    print(f"Images: {root}")


if __name__ == "__main__":
    main()
