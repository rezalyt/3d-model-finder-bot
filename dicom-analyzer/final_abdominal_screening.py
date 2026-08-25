#!/usr/bin/env python3
"""Unified research screening for multiphase abdominal CT.

Uses existing TotalSegmentator masks; no repeat segmentation. Produces one
screening report covering solid organs, gallbladder, stomach and bowel.
This is a candidate generator for review, not a medical diagnosis.
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

SOLID = [
    "liver", "pancreas", "spleen", "kidney_left", "kidney_right",
    "adrenal_gland_left", "adrenal_gland_right", "gallbladder",
]
HOLLOW = ["stomach", "duodenum", "small_bowel", "colon"]
VESSELS = ["aorta", "inferior_vena_cava", "portal_vein_and_splenic_vein"]
ALL_ANALYZED = SOLID + HOLLOW
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
    return img, img.get_fdata(dtype=np.float32)


def load_mask(seg_dir, name, shape):
    path = Path(seg_dir) / f"{name}.nii.gz"
    if not path.exists():
        return None
    mask = np.asarray(nib.load(str(path)).get_fdata()) > 0.5
    return mask if mask.shape == shape else None


def connected_candidates(vol, region, organ, voxel_mm3, wall=False):
    base = ndimage.binary_erosion(region, iterations=2) if not wall else (region & ~ndimage.binary_erosion(region, iterations=2))
    if int(base.sum()) < 300:
        return []
    inside = vol[base]
    med = float(np.median(inside))
    mad = float(np.median(np.abs(inside - med))) + 1e-3
    smooth = ndimage.gaussian_filter(vol, sigma=1.0)
    score = np.abs(smooth - med) / (1.4826 * mad)
    delta = np.abs(smooth - med)
    cand = base & (score >= 3.0) & (delta >= (30.0 if wall else 25.0))
    cand = ndimage.binary_opening(cand, iterations=1)
    cand = ndimage.binary_closing(cand, iterations=1)
    labels, n = ndimage.label(cand, structure=np.ones((3, 3, 3), dtype=np.uint8))
    out = []
    for label in range(1, n + 1):
        idx = labels == label
        vox = int(idx.sum())
        volume = vox * voxel_mm3
        if volume < (120 if wall else 80) or volume > 50000:
            continue
        z, y, x = np.where(idx)
        vals = vol[idx]
        out.append({
            "organ": organ,
            "region_type": "wall" if wall else "parenchymal",
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
    return out[:30]


def dist(a, b):
    return float(np.linalg.norm(np.array(a["centroid_zyx"]) - np.array(b["centroid_zyx"])))


def triage(g):
    organ = g["organ"]
    obs = g["observations"]
    phases = {o["phase"] for o in obs}
    score = 30 + 10 * (len(phases) - 1) + 8 * max(o["deviation_score"] for o in obs)
    reasons = []
    if organ.startswith("kidney"):
        delayed = [float(o["mean_hu"]) for o in obs if o["phase"] == "delayed"]
        portal = [float(o["mean_hu"]) for o in obs if o["phase"] == "portal"]
        if delayed and portal and max(delayed) >= 180 and max(portal) <= 100:
            score -= 55
            reasons.append("Pattern may reflect delayed contrast excretion/collecting-system effect.")
    if organ == "liver":
        portal = [float(o["mean_hu"]) for o in obs if o["phase"] == "portal"]
        arterial = [float(o["mean_hu"]) for o in obs if o["phase"] == "arterial"]
        if portal and 130 <= max(portal) <= 180:
            score -= 30
            reasons.append("Portal enhancement range can reflect vascular/background enhancement.")
        if arterial and 90 <= max(arterial) <= 130:
            score -= 20
            reasons.append("Arterial enhancement can reflect vascular/background enhancement.")
    if organ in HOLLOW:
        score -= 10
        reasons.append("Hollow-organ wall signal is sensitive to distension and contents; manual review is required.")
    if len(phases) == 1:
        score -= 5
        reasons.append("Only one phase observed.")
    elif len(phases) >= 3:
        reasons.append("Observed across multiple phases; reproducibility supports review.")
    score = max(0.0, min(100.0, score))
    level = "HIGH_REVIEW" if score >= 60 else "REVIEW" if score >= 25 else "LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY"
    if not reasons:
        reasons.append("No strong rule-based context filter applied.")
    return level, round(score, 1), reasons


def group(candidates):
    groups = []
    for c in sorted(candidates, key=lambda x: x["deviation_score"], reverse=True):
        placed = False
        for g in groups:
            if c["organ"] == g["organ"] and any(dist(c, o) <= 15.0 for o in g["observations"]):
                g["observations"].append(c)
                placed = True
                break
        if not placed:
            groups.append({"organ": c["organ"], "observations": [c]})
    for i, g in enumerate(groups, 1):
        phases = sorted({x["phase"] for x in g["observations"]})
        g["candidate_id"] = f"F{i:03d}"
        g["phases"] = phases
        g["phase_count"] = len(phases)
        g["max_deviation_score"] = round(max(x["deviation_score"] for x in g["observations"]), 2)
        level, score, reasons = triage(g)
        g["triage_level"] = level
        g["triage_score"] = score
        g["triage_reasons"] = reasons
    return sorted(groups, key=lambda x: x["triage_score"], reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip", type=Path)
    ap.add_argument("--out", type=Path, default=Path("dicom-ai-result/final_screening"))
    args = ap.parse_args()
    if not args.zip.exists():
        raise SystemExit(f"ZIP not found: {args.zip}")
    root = args.out
    root.mkdir(parents=True, exist_ok=True)
    phase_map = {}
    for uid, items in inventory(args.zip).items():
        if len(items) < 25 or any(w in items[0]["description"].lower() for w in SKIP):
            continue
        phase = classify_phase(items[0]["description"])
        if phase != "other" and (phase not in phase_map or len(items) > len(phase_map[phase]["items"])):
            safe = safe_name(items[0]["description"])
            matches = sorted(Path("dicom-ai-result").glob(f"series_*_{safe}"))
            if matches:
                phase_map[phase] = {"items": items, "description": items[0]["description"], "seg": matches[0] / "segmentation"}
    if not phase_map:
        raise SystemExit("No completed phase segmentations found")

    candidates = []
    for phase, s in sorted(phase_map.items()):
        with tempfile.TemporaryDirectory(prefix="final_screen_") as td:
            img, vol = reconstruct(args.zip, s["items"], Path(td))
            voxel_mm3 = float(np.prod(img.header.get_zooms()[:3]))
            vessel_union = np.zeros(vol.shape, dtype=bool)
            for vessel in VESSELS:
                m = load_mask(s["seg"], vessel, vol.shape)
                if m is not None:
                    vessel_union |= ndimage.binary_dilation(m, iterations=2)
            for organ in ALL_ANALYZED:
                mask = load_mask(s["seg"], organ, vol.shape)
                if mask is None:
                    continue
                wall = organ in HOLLOW
                region = mask & ~vessel_union
                for c in connected_candidates(vol, region, organ, voxel_mm3, wall=wall):
                    c["phase"] = phase
                    c["series"] = s["description"]
                    candidates.append(c)
        print(f"[OK] Final screening {phase}: {s['description']}")

    groups = group(candidates)
    summary = {
        "series": sorted(phase_map.keys()),
        "candidate_count": len(groups),
        "high_review": sum(g["triage_level"] == "HIGH_REVIEW" for g in groups),
        "review": sum(g["triage_level"] == "REVIEW" for g in groups),
        "low_priority": sum(g["triage_level"] == "LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY" for g in groups),
        "organs_covered": ALL_ANALYZED,
    }
    report = {"type": "unified_abdominal_research_screening", "warning": "Candidate generation and rule-based triage only; not a medical diagnosis or exclusion of disease.", "summary": summary, "candidates": groups}
    (root / "final_screening.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (root / "final_screening.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "organ", "region_type", "phases", "triage_level", "triage_score", "volume_mm3", "observations", "reasons"])
        for g in groups:
            region = g["observations"][0].get("region_type", "")
            obs = "; ".join(f"{o['phase']}={o['mean_hu']:.1f}HU" for o in g["observations"])
            w.writerow([g["candidate_id"], g["organ"], region, ",".join(g["phases"]), g["triage_level"], g["triage_score"], g["observations"][0].get("volume_mm3"), obs, " | ".join(g["triage_reasons"])])
    rows = []
    for g in groups:
        obs = ", ".join(f"{o['phase']}: {o['mean_hu']:.0f} HU" for o in g["observations"])
        rows.append(f"<tr><td>{html.escape(g['candidate_id'])}</td><td>{html.escape(g['organ'])}</td><td>{g['triage_level']}</td><td>{g['triage_score']:.1f}</td><td>{obs}</td><td>{html.escape(' '.join(g['triage_reasons']))}</td></tr>")
    text = "<!doctype html><html><head><meta charset='utf-8'><title>Unified abdominal CT screening</title><style>body{font-family:Arial,sans-serif;margin:30px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:7px;text-align:left;vertical-align:top}</style></head><body><h1>Unified abdominal CT screening</h1><p><b>Research/screening only.</b> Algorithmic candidate generation and triage are not a diagnosis.</p><p>Series: %s &nbsp; Candidates: %d &nbsp; HIGH_REVIEW: %d &nbsp; REVIEW: %d &nbsp; Low priority: %d</p><table><tr><th>ID</th><th>Organ</th><th>Level</th><th>Score</th><th>Observations</th><th>Reason</th></tr>%s</table></body></html>" % (", ".join(summary["series"]), summary["candidate_count"], summary["high_review"], summary["review"], summary["low_priority"], "".join(rows))
    (root / "final_screening.html").write_text(text, encoding="utf-8")
    print(f"Completed unified screening: {summary['candidate_count']} candidates")
    print(f"HIGH_REVIEW: {summary['high_review']}")
    print(f"REVIEW: {summary['review']}")
    print(f"LOW_PRIORITY: {summary['low_priority']}")
    print(f"JSON: {root / 'final_screening.json'}")
    print(f"HTML: {root / 'final_screening.html'}")


if __name__ == "__main__":
    main()
