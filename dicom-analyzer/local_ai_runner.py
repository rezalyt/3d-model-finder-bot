#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pydicom

ROIS = [
    "liver", "pancreas", "gallbladder", "spleen",
    "kidney_left", "kidney_right",
    "adrenal_gland_left", "adrenal_gland_right", "aorta",
]
SKIP = ("topogram", "scout", "localizer", "monitor", "premonitor")


def inventory(zip_path):
    series = {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = Path(info.filename).parts
            if Path(info.filename).is_absolute() or ".." in parts:
                continue
            try:
                with z.open(info) as fh:
                    ds = pydicom.dcmread(fh, stop_before_pixels=True, force=True)
                if not hasattr(ds, "Rows") or not hasattr(ds, "Columns"):
                    continue
                uid = str(getattr(ds, "SeriesInstanceUID", "unknown"))
                series.setdefault(uid, []).append({
                    "name": info.filename,
                    "instance": int(getattr(ds, "InstanceNumber", 0) or 0),
                    "description": str(getattr(ds, "SeriesDescription", "")),
                    "modality": str(getattr(ds, "Modality", "")),
                    "body_part": str(getattr(ds, "BodyPartExamined", "")),
                })
            except Exception:
                continue
    for items in series.values():
        items.sort(key=lambda x: (x["instance"], x["name"]))
    return series


def copy_series(zip_path, items, dst):
    dst.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for n, item in enumerate(items, 1):
            target = dst / f"slice_{n:05d}.dcm"
            with z.open(item["name"]) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=4 * 1024 * 1024)


def has_gpu():
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def run_series(series_dir, out_dir, gpu):
    # GPU: prefer full-resolution v3/big for quality.
    # CPU: use v3/small + fast to fit normal desktops more reliably.
    cmd = [
        "TotalSegmentator",
        "-i", str(series_dir),
        "-o", str(out_dir),
        "--task", "total_v3",
        "--roi_subset", *ROIS,
        "--statistics",
        "--statistics_extra",
        "--ml",
        "--quiet",
    ]
    if gpu:
        cmd += ["--device", "gpu", "--model_size", "big"]
    else:
        cmd += [
            "--device", "cpu",
            "--model_size", "small",
            "--fast",
            "--body_seg",
            "--force_split",
            "--nr_thr_resamp", "1",
            "--nr_thr_saving", "1",
        ]
    return subprocess.run(cmd, text=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser(description="Sequential local DICOM AI analysis")
    ap.add_argument("zip", type=Path, help="DICOM ZIP archive")
    ap.add_argument("--out", type=Path, default=Path("dicom-ai-result"))
    args = ap.parse_args()
    if not args.zip.exists():
        raise SystemExit(f"ZIP not found: {args.zip}")

    args.out.mkdir(parents=True, exist_ok=True)
    series = inventory(args.zip)
    eligible = []
    for uid, items in series.items():
        if len(items) < 25:
            continue
        base = items[0]
        if base["modality"] != "CT":
            continue
        desc = base["description"].lower()
        if any(x in desc for x in SKIP):
            continue
        eligible.append((len(items), uid, items))
    eligible.sort(reverse=True)
    if not eligible:
        raise SystemExit("No suitable diagnostic CT series found")

    gpu = has_gpu()
    report = {
        "source": str(args.zip),
        "device": "gpu" if gpu else "cpu",
        "model": "TotalSegmentator 2.18.0 / total_v3",
        "rois": ROIS,
        "series": [],
        "note": "Segmentation/quantification only; not a medical diagnosis.",
    }

    for index, (count, uid, items) in enumerate(eligible, 1):
        name = items[0]["description"] or f"series_{index:03d}"
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:80]
        series_out = args.out / f"series_{index:03d}_{safe}"
        with tempfile.TemporaryDirectory(prefix="dicom_series_") as tmp:
            dicom_dir = Path(tmp) / "dicom"
            copy_series(args.zip, items, dicom_dir)
            proc = run_series(dicom_dir, series_out, gpu)
            stdout = proc.stdout[-6000:]
            stderr = proc.stderr[-6000:]
            entry = {
                "series_index": index,
                "series_uid": uid,
                "description": name,
                "instances": count,
                "returncode": proc.returncode,
                "result_dir": str(series_out),
                "stdout_tail": stdout,
                "stderr_tail": stderr,
                "status": "completed" if proc.returncode == 0 else "failed",
            }
            report["series"].append(entry)
            (series_out / "run.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
            if proc.returncode != 0:
                print(f"[FAILED] Series {index}: {name}", file=sys.stderr)
            else:
                print(f"[OK] Series {index}: {name} ({count} slices)")

    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for x in report["series"] if x["status"] == "completed")
    print(f"Completed: {ok}/{len(report['series'])} diagnostic series")
    print(f"Report: {args.out / 'report.json'}")


if __name__ == "__main__":
    main()
