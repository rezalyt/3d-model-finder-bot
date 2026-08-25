#!/usr/bin/env python3
"""Automatic visual verification pass for HIGH_REVIEW CT screening candidates.

Research/screening only. This tool creates reproducible PNG overlays and a JSON
summary for the candidates selected by screening_v2_pipeline.py. It does not
make or imply a medical diagnosis.
"""
import argparse
import csv
import json
import math
import shutil
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pydicom
import nibabel as nib
from PIL import Image, ImageDraw, ImageFont
import dicom2nifti

PHASES = {"native", "arterial", "portal", "delayed"}
SKIP = ("topogram", "scout", "localizer", "monitor", "premonitor")


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


def safe_name(s):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s).strip("_")[:90] or "series"


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
    vol = img.get_fdata(dtype=np.float32)
    return img, vol


def normalize_slice(arr, center=40.0, width=400.0):
    lo = center - width / 2.0
    hi = center + width / 2.0
    x = np.clip((arr - lo) / max(hi - lo, 1.0), 0.0, 1.0)
    return (x * 255.0).astype(np.uint8)


def crop(arr, cy, cx, size=180):
    h, w = arr.shape
    half = size // 2
    y0 = max(0, min(h - size, int(round(cy)) - half)) if h > size else 0
    x0 = max(0, min(w - size, int(round(cx)) - half)) if w > size else 0
    crop = arr[y0:min(y0 + size, h), x0:min(x0 + size, w)]
    return crop, y0, x0


def make_panel(vol, obs, title, out_path, phase):
    z, y, x = [float(v) for v in obs["centroid_zyx"]]
    zi = int(round(z))
    zi = max(0, min(vol.shape[0] - 1, zi))
    axial = vol[zi, :, :]
    image, y0, x0 = crop(axial, y, x)
    base = Image.fromarray(normalize_slice(image), mode="L").convert("RGB")
    draw = ImageDraw.Draw(base)
    px = int(round(x)) - x0
    py = int(round(y)) - y0
    r = max(5, min(14, int(round(max(image.shape) / 22))))
    draw.ellipse((px - r, py - r, px + r, py + r), outline=(255, 0, 0), width=3)
    draw.line((px - 2 * r, py, px + 2 * r, py), fill=(255, 0, 0), width=1)
    draw.line((px, py - 2 * r, px, py + 2 * r), fill=(255, 0, 0), width=1)
    canvas = Image.new("RGB", (base.width, base.height + 44), (25, 25, 25))
    canvas.paste(base, (0, 44))
    draw2 = ImageDraw.Draw(canvas)
    draw2.text((8, 7), title, fill=(255, 255, 255))
    draw2.text((8, 24), f"phase={phase}  z={zi}  HU={obs.get('mean_hu', 'n/a')}", fill=(220, 220, 220))
    canvas.save(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip", type=Path)
    ap.add_argument("--input", type=Path, default=Path("dicom-ai-result/screening_v2.json"))
    ap.add_argument("--out", type=Path, default=Path("dicom-ai-result/screening_v3"))
    ap.add_argument("--padding", type=int, default=2)
    args = ap.parse_args()
    if not args.zip.exists():
        raise SystemExit(f"ZIP not found: {args.zip}")
    if not args.input.exists():
        raise SystemExit(f"Input screening JSON not found: {args.input}")

    report = json.loads(args.input.read_text(encoding="utf-8"))
    high = [c for c in report.get("candidates", []) if c.get("triage_level") == "HIGH_REVIEW"]
    if not high:
        raise SystemExit("No HIGH_REVIEW candidates found in screening_v2.json")

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

    output_candidates = []
    for candidate in high:
        cid = str(candidate.get("candidate_id", "candidate"))
        cdir = root / cid
        cdir.mkdir(parents=True, exist_ok=True)
        generated = []
        errors = []
        obs_by_phase = {str(o.get("phase")): o for o in candidate.get("observations", [])}
        for phase in sorted(PHASES):
            series = phase_map.get(phase)
            if not series:
                continue
            obs = obs_by_phase.get(phase)
            if obs is None:
                # Show same anatomical centroid in the missing phase when the candidate
                # is cross-phase grouped, using the best available observation.
                obs = candidate.get("observations", [None])[0]
                if obs is None:
                    continue
            try:
                with tempfile.TemporaryDirectory(prefix="screening_v3_") as td:
                    img, vol = reconstruct(args.zip, series["items"], Path(td))
                    base_obs = dict(obs)
                    path = cdir / f"{phase}.png"
                    make_panel(vol, base_obs, f"{cid} | {candidate.get('organ')} | {phase}", path, phase)
                    generated.append({"phase": phase, "path": str(path), "series": series["description"], "shape": list(vol.shape), "zoom": list(img.header.get_zooms()[:3])})
            except Exception as exc:
                errors.append({"phase": phase, "error": str(exc)})
        entry = {
            "candidate_id": cid,
            "organ": candidate.get("organ"),
            "triage_level": candidate.get("triage_level"),
            "triage_score": candidate.get("triage_score"),
            "screening_priority": candidate.get("screening_priority"),
            "triage_reasons": candidate.get("triage_reasons", []),
            "observations": candidate.get("observations", []),
            "overlays": generated,
            "errors": errors,
        }
        (cdir / "candidate.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        output_candidates.append(entry)
        print(f"[OK] {cid}: {len(generated)} overlay(s)")

    result = {
        "type": "research_screening_v3_visual_review",
        "warning": "Visual overlay generation only; not a medical diagnosis.",
        "summary": {"high_review_input": len(high), "candidates_processed": len(output_candidates), "overlays_generated": sum(len(x["overlays"]) for x in output_candidates)},
        "candidates": output_candidates,
    }
    (root / "screening_v3.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with (root / "screening_v3.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["candidate_id", "organ", "triage_score", "triage_level", "phases_generated", "errors"])
        for c in output_candidates:
            w.writerow([c["candidate_id"], c["organ"], c["triage_score"], c["triage_level"], ",".join(x["phase"] for x in c["overlays"]), " | ".join(e["phase"] + ":" + e["error"] for e in c["errors"])])
    print(f"Completed v3 visual review: {len(output_candidates)} candidates, {result['summary']['overlays_generated']} overlays")
    print(f"JSON: {root / 'screening_v3.json'}")
    print(f"Images: {root}")


if __name__ == "__main__":
    main()
