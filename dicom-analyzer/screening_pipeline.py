#!/usr/bin/env python3
"""Local CT screening layer built on existing TotalSegmentator results.

This is a research/screening candidate generator, NOT a medical diagnosis tool.
It re-reads DICOM from source.zip, reconstructs the volume with dicom2nifti,
loads existing organ masks, detects suspicious intra-organ regions by robust
local intensity deviation, and compares candidates across CT phases by centroid.
"""
import argparse
import csv
import html
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pydicom
import nibabel as nib
from scipy import ndimage
import dicom2nifti

ORGANS = [
    "liver", "pancreas", "gallbladder", "spleen",
    "kidney_left", "kidney_right", "adrenal_gland_left", "adrenal_gland_right",
]
SKIP = ("topogram", "scout", "localizer", "monitor", "premonitor")
PHASES = {
    "native": ("non contrast", "non-contrast", "noncontrast", "native", "without contrast"),
    "arterial": ("arterial",),
    "portal": ("portal", "venous", "porto"),
    "delayed": ("delayed",),
}


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
    for phase, words in PHASES.items():
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


def load_mask(seg_dir, organ, shape):
    path = Path(seg_dir) / f"{organ}.nii.gz"
    if not path.exists():
        return None
    mask = np.asarray(nib.load(str(path)).get_fdata()) > 0.5
    if mask.shape != shape:
        return None
    return mask


def robust_candidates(vol, mask, organ, voxel_mm3):
    # Remove a thin boundary layer so normal edge/partial-volume effects dominate less.
    inner = ndimage.binary_erosion(mask, iterations=2)
    if inner.sum() < 500:
        return []
    inside = vol[inner]
    med = float(np.median(inside))
    mad = float(np.median(np.abs(inside - med))) + 1e-3
    smooth = ndimage.gaussian_filter(vol, sigma=1.0)
    score = np.abs(smooth - med) / (1.4826 * mad)
    # Candidate voxels require both a robust deviation and a meaningful absolute CT difference.
    delta = np.abs(smooth - med)
    cand = inner & (score >= 3.0) & (delta >= 25.0)
    cand = ndimage.binary_opening(cand, iterations=1)
    cand = ndimage.binary_closing(cand, iterations=1)
    labels, n = ndimage.label(cand, structure=np.ones((3, 3, 3), dtype=np.uint8))
    out = []
    for label in range(1, n + 1):
        idx = labels == label
        vox = int(idx.sum())
        volume = vox * voxel_mm3
        if volume < 80 or volume > 50000:
            continue
        z, y, x = np.where(idx)
        vals = vol[idx]
        out.append({
            "organ": organ,
            "voxel_count": vox,
            "volume_mm3": round(volume, 1),
            "centroid_zyx": [round(float(z.mean()), 2), round(float(y.mean()), 2), round(float(x.mean()), 2)],
            "min_hu": round(float(vals.min()), 1),
            "max_hu": round(float(vals.max()), 1),
            "mean_hu": round(float(vals.mean()), 1),
            "median_hu": round(float(np.median(vals)), 1),
            "deviation_score": round(float(score[idx].max()), 2),
        })
    out.sort(key=lambda x: (x["deviation_score"], x["volume_mm3"]), reverse=True)
    return out[:40]


def phase_distance(a, b):
    return float(np.linalg.norm(np.array(a["centroid_zyx"]) - np.array(b["centroid_zyx"])))


def group_cross_phase(candidates):
    groups = []
    for c in sorted(candidates, key=lambda x: x.get("deviation_score", 0), reverse=True):
        placed = False
        for g in groups:
            if c["organ"] != g["organ"]:
                continue
            if any(phase_distance(c, x) <= 15.0 for x in g["observations"]):
                g["observations"].append(c)
                placed = True
                break
        if not placed:
            groups.append({"organ": c["organ"], "observations": [c]})
    for i, g in enumerate(groups, 1):
        phases = {x["phase"] for x in g["observations"]}
        g["candidate_id"] = f"C{i:03d}"
        g["phase_count"] = len(phases)
        g["phases"] = sorted(phases)
        scores = [x["deviation_score"] for x in g["observations"]]
        g["max_deviation_score"] = round(max(scores), 2)
        # More independent phase observations increase priority, but do not imply diagnosis.
        g["screening_priority"] = round(min(100.0, 30 + 12 * (len(phases) - 1) + 8 * max(scores)), 1)
    return sorted(groups, key=lambda g: g["screening_priority"], reverse=True)


def render_html(path, report):
    rows = []
    for c in report["candidates"]:
        obs = ", ".join(f"{o['phase']}: {o['mean_hu']:.0f} HU" for o in c["observations"])
        rows.append(f"<tr><td>{html.escape(c['candidate_id'])}</td><td>{html.escape(c['organ'])}</td><td>{c['phase_count']}</td><td>{c['max_deviation_score']:.2f}</td><td>{obs}</td></tr>")
    body = "".join(rows) or '<tr><td colspan="5">No screening candidates</td></tr>'
    text = f"""<!doctype html><html><head><meta charset='utf-8'><title>CT screening</title>
<style>body{{font-family:Arial,sans-serif;margin:30px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:7px;text-align:left}}</style></head>
<body><h1>CT screening candidate report</h1><p><b>Research/screening only.</b> The candidates below are algorithmic abnormalities for review, not diagnoses.</p>
<p>Series analysed: {report['summary']['series_completed']} &nbsp; Candidates: {len(report['candidates'])}</p>
<table><tr><th>ID</th><th>Organ</th><th>Phases</th><th>Priority</th><th>Observations</th></tr>{body}</table></body></html>"""
    path.write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip", type=Path)
    ap.add_argument("--out", type=Path, default=Path("dicom-ai-result"))
    args = ap.parse_args()
    if not args.zip.exists():
        raise SystemExit(f"ZIP not found: {args.zip}")
    root = args.out
    root.mkdir(parents=True, exist_ok=True)
    series = []
    for uid, items in inventory(args.zip).items():
        if len(items) < 25:
            continue
        desc = items[0]["description"]
        if any(w in desc.lower() for w in SKIP):
            continue
        safe = safe_name(desc)
        matches = sorted(root.glob(f"series_*_{safe}"))
        seg = matches[0] / "segmentation" if matches else None
        if seg and seg.exists():
            series.append({"uid": uid, "items": items, "description": desc, "phase": classify_phase(desc), "seg": seg})
    # Prefer diagnostic 1.5-mm series and cap duplicates per phase.
    phase_map = {}
    for s in series:
        p = s["phase"]
        if p == "other":
            continue
        if p not in phase_map or len(s["items"]) > len(phase_map[p]["items"]):
            phase_map[p] = s
    selected = list(phase_map.values())
    state_path = root / "screening_state.json"
    state = {"status": "running", "series": [], "candidates": []}
    all_candidates = []
    for s in selected:
        with tempfile.TemporaryDirectory(prefix="screening_") as td:
            img, vol = reconstruct(args.zip, s["items"], Path(td))
            zooms = img.header.get_zooms()[:3]
            voxel_mm3 = float(np.prod(zooms))
            for organ in ORGANS:
                mask = load_mask(s["seg"], organ, vol.shape)
                if mask is None:
                    continue
                for c in robust_candidates(vol, mask, organ, voxel_mm3):
                    c["phase"] = s["phase"]
                    c["series"] = s["description"]
                    all_candidates.append(c)
        state["series"].append({"phase": s["phase"], "description": s["description"], "status": "completed"})
        state["candidates_found"] = len(all_candidates)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] Screening {s['phase']}: {s['description']} -> {sum(1 for x in all_candidates if x['phase']==s['phase'])} candidates")
    groups = group_cross_phase(all_candidates)
    report = {
        "type": "research_screening",
        "warning": "Algorithmic candidate generation only; not a medical diagnosis.",
        "summary": {"series_completed": len(selected), "candidates": len(groups)},
        "candidates": groups,
    }
    (root / "screening.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (root / "screening.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["candidate_id", "organ", "phase_count", "phases", "priority", "max_deviation_score", "observations"])
        for c in groups:
            obs = "; ".join(f"{o['phase']}={o['mean_hu']:.1f}HU" for o in c['observations'])
            w.writerow([c['candidate_id'], c['organ'], c['phase_count'], ",".join(c['phases']), c['screening_priority'], c['max_deviation_score'], obs])
    render_html(root / "screening.html", report)
    state.update({"status": "completed", "candidates": groups, "summary": report["summary"]})
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Completed screening: {len(groups)} candidates")
    print(f"JSON: {root / 'screening.json'}")
    print(f"HTML: {root / 'screening.html'}")


if __name__ == "__main__":
    main()
