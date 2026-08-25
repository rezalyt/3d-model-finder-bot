#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pydicom

# Coverage for unified abdominal screening. TotalSegmentator's total task
# includes these anatomy classes, including stomach, gallbladder, small bowel,
# duodenum and colon.
ROIS = [
    "liver", "pancreas", "gallbladder", "spleen",
    "kidney_left", "kidney_right",
    "adrenal_gland_left", "adrenal_gland_right",
    "stomach", "duodenum", "small_bowel", "colon",
    "aorta", "inferior_vena_cava", "portal_vein_and_splenic_vein",
]
SKIP = ("topogram", "scout", "localizer", "monitor", "premonitor")


def inventory(zip_path: Path):
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


def copy_series(zip_path: Path, items, dst: Path):
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


def run_series(series_dir: Path, out_dir: Path, gpu: bool):
    cmd = [
        "TotalSegmentator", "-i", str(series_dir), "-o", str(out_dir),
        "--task", "total", "--roi_subset", *ROIS,
        "--statistics", "--statistics_extra", "--quiet",
        "--nr_thr_resamp", "1", "--nr_thr_saving", "1",
    ]
    cmd += ["--device", "gpu" if gpu else "cpu"]
    if not gpu:
        cmd += ["--fast", "--body_seg", "--force_split"]
    return subprocess.run(cmd, text=True, capture_output=True)


def safe_name(name: str, fallback: str):
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_")
    return (cleaned or fallback)[:100]


def existing_ok(series_out: Path):
    run_file = series_out / "run.json"
    stats_file = series_out / "segmentation" / "statistics.json"
    if not run_file.exists() or not stats_file.exists():
        return False
    try:
        data = json.loads(run_file.read_text(encoding="utf-8"))
        if data.get("status") != "completed" or data.get("returncode") != 0:
            return False
        seg = series_out / "segmentation"
        return all((seg / f"{roi}.nii.gz").exists() for roi in ROIS)
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Resumable one-command local DICOM AI pipeline")
    ap.add_argument("zip", type=Path)
    ap.add_argument("--out", type=Path, default=Path("dicom-ai-result"))
    args = ap.parse_args()
    if not args.zip.exists():
        raise SystemExit(f"ZIP not found: {args.zip}")

    args.out.mkdir(parents=True, exist_ok=True)
    gpu = has_gpu()
    all_series = inventory(args.zip)
    eligible = []
    for uid, items in all_series.items():
        if len(items) < 25:
            continue
        base = items[0]
        if base["modality"] != "CT":
            continue
        desc = base["description"].lower()
        if any(word in desc for word in SKIP):
            continue
        eligible.append((len(items), uid, items))
    eligible.sort(reverse=True)

    manifest = {
        "source": str(args.zip),
        "device": "gpu" if gpu else "cpu",
        "model": "TotalSegmentator 2.18.0 / total",
        "rois": ROIS,
        "series": [],
    }

    for index, (count, uid, items) in enumerate(eligible, 1):
        description = items[0]["description"] or f"series_{index:03d}"
        series_out = args.out / f"series_{index:03d}_{safe_name(description, f'series_{index:03d}')}"
        if existing_ok(series_out):
            entry = json.loads((series_out / "run.json").read_text(encoding="utf-8"))
            entry["skipped"] = True
            manifest["series"].append(entry)
            print(f"[SKIP] Series {index}: {description} ({count} slices) - already completed")
            continue

        series_out.mkdir(parents=True, exist_ok=True)
        entry = {
            "series_index": index,
            "series_uid": uid,
            "description": description,
            "instances": count,
            "status": "running",
            "returncode": None,
            "result_dir": str(series_out),
            "required_rois": ROIS,
        }
        (series_out / "run.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")

        with tempfile.TemporaryDirectory(prefix="dicom_series_") as tmp:
            dicom_dir = Path(tmp) / "dicom"
            copy_series(args.zip, items, dicom_dir)
            proc = run_series(dicom_dir, series_out / "segmentation", gpu)
            entry.update({
                "returncode": proc.returncode,
                "status": "completed" if proc.returncode == 0 else "failed",
                "stdout_tail": proc.stdout[-6000:],
                "stderr_tail": proc.stderr[-6000:],
            })

        (series_out / "run.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["series"].append(entry)
        state = "OK" if entry["status"] == "completed" else "FAILED"
        print(f"[{state}] Series {index}: {description} ({count} slices)")
        if entry["status"] == "failed":
            print("       Pipeline will continue; rerun will resume from this series.")

        (args.out / "pipeline_state.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    completed = sum(1 for x in manifest["series"] if x.get("status") == "completed")
    failed = sum(1 for x in manifest["series"] if x.get("status") == "failed")
    manifest["summary"] = {"total": len(manifest["series"]), "completed": completed, "failed": failed}
    (args.out / "pipeline_state.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Completed: {completed}/{len(manifest['series'])}; failed: {failed}")
    print(f"State: {args.out / 'pipeline_state.json'}")


if __name__ == "__main__":
    main()
