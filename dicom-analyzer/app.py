import io
import json
import os
import re
import shutil
import statistics
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pydicom
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from PIL import Image, ImageOps, ImageDraw

app = FastAPI(title="DICOM Analyzer", version="0.1.0")
SOURCE_URL = os.getenv("DICOM_SOURCE_URL", "")
WORK_ROOT = Path(os.getenv("WORK_ROOT", "/tmp/dicom-analyzer"))
WORK_ROOT.mkdir(parents=True, exist_ok=True)


def drive_file_id(url: str) -> str | None:
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    q = parse_qs(urlparse(url).query)
    return q.get("id", [None])[0]


def download_drive(url: str, out_path: Path) -> None:
    fid = drive_file_id(url)
    candidates = []
    if fid:
        candidates = [
            f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t",
            f"https://drive.google.com/uc?export=download&id={fid}&confirm=t",
        ]
    candidates.append(url)

    session = requests.Session()
    last_error = None
    for u in candidates:
        try:
            r = session.get(u, stream=True, timeout=(20, 180), allow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            # Large Google Drive files may first return an HTML confirmation page.
            if "text/html" in ctype:
                data = r.content
                text = data.decode("utf-8", errors="ignore")
                token = re.search(r"confirm=([0-9A-Za-z_-]+)", text)
                if token and fid:
                    u2 = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm={token.group(1)}"
                    r = session.get(u2, stream=True, timeout=(20, 180), allow_redirects=True,
                                    headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                    ctype = (r.headers.get("content-type") or "").lower()
                if "text/html" in ctype:
                    last_error = RuntimeError("Google Drive returned an HTML confirmation/access page instead of the ZIP")
                    continue
            with out_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
            if out_path.stat().st_size > 0:
                return
        except Exception as e:
            last_error = e
    raise RuntimeError(f"Unable to download source archive: {last_error}")


def normalize_pixels(ds) -> np.ndarray | None:
    try:
        arr = ds.pixel_array.astype(np.float32)
        slope = float(getattr(ds, "RescaleSlope", 1.0))
        intercept = float(getattr(ds, "RescaleIntercept", 0.0))
        arr = arr * slope + intercept
        # Multi-frame is reduced to a representative frame for preview.
        if arr.ndim > 2:
            arr = arr[0]
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        return arr
    except Exception:
        return None


def preview_image(arr: np.ndarray, size=(320, 320)) -> Image.Image:
    a = arr.astype(np.float32)
    lo, hi = np.percentile(a, [1, 99]) if a.size else (0, 1)
    if hi <= lo:
        hi = lo + 1
    a = np.clip((a - lo) / (hi - lo), 0, 1)
    img = Image.fromarray((a * 255).astype(np.uint8), mode="L")
    return ImageOps.fit(img, size, method=Image.Resampling.BILINEAR)


def analyze_zip(zip_path: Path, job_dir: Path) -> dict:
    extracted = job_dir / "dicom"
    extracted.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        # Protect the service from path traversal and absurd ZIPs.
        safe = []
        total_uncompressed = 0
        for m in members:
            p = Path(m.filename)
            if p.is_absolute() or ".." in p.parts:
                continue
            total_uncompressed += m.file_size
            if total_uncompressed > 25 * 1024**3:
                raise RuntimeError("Archive exceeds the 25 GB processing safety limit")
            safe.append(m)
        zf.extractall(extracted, members=safe)

    records = []
    for path in extracted.rglob("*"):
        if not path.is_file():
            continue
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=False, force=True)
            if not hasattr(ds, "SOPClassUID") and not hasattr(ds, "PixelData"):
                continue
            series_uid = str(getattr(ds, "SeriesInstanceUID", "unknown"))
            instance = int(getattr(ds, "InstanceNumber", 0) or 0)
            arr = normalize_pixels(ds)
            records.append({
                "path": str(path),
                "series_uid": series_uid,
                "series_number": int(getattr(ds, "SeriesNumber", 0) or 0),
                "instance": instance,
                "rows": int(getattr(ds, "Rows", 0) or 0),
                "cols": int(getattr(ds, "Columns", 0) or 0),
                "modality": str(getattr(ds, "Modality", "")),
                "body_part": str(getattr(ds, "BodyPartExamined", "")),
                "series_desc": str(getattr(ds, "SeriesDescription", "")),
                "study_desc": str(getattr(ds, "StudyDescription", "")),
                "arr": arr,
            })
        except Exception:
            continue

    if not records:
        raise RuntimeError("No readable DICOM objects with image data were found")

    series = {}
    for r in records:
        series.setdefault(r["series_uid"], []).append(r)
    for items in series.values():
        items.sort(key=lambda x: (x["instance"], x["path"]))

    summaries = []
    img_dir = job_dir / "images"
    img_dir.mkdir(exist_ok=True)
    all_candidates = []

    for idx, (uid, items) in enumerate(series.items(), start=1):
        with_pixels = [x for x in items if x["arr"] is not None]
        if not with_pixels:
            continue
        # Sample at most 48 frames for a compact contact sheet.
        n = len(with_pixels)
        take = min(48, n)
        positions = np.linspace(0, n - 1, take).round().astype(int)
        thumbs = []
        deviations = []
        areas = []
        for pos in positions:
            rec = with_pixels[int(pos)]
            arr = rec["arr"]
            thumb = preview_image(arr)
            thumbs.append(thumb)
            p1, p99 = np.percentile(arr, [1, 99])
            deviations.append(float(np.std(np.clip(arr, p1, p99))))
            # Very rough foreground fraction; not a diagnostic test.
            lo, hi = np.percentile(arr, [5, 95])
            frac = float(np.mean(arr > (lo + 0.15 * (hi - lo)))) if hi > lo else 0.0
            areas.append(frac)

        med_dev = statistics.median(deviations) if deviations else 0.0
        med_area = statistics.median(areas) if areas else 0.0
        cand = []
        for k, (dev, area) in enumerate(zip(deviations, areas)):
            score = 0.0
            if med_dev and dev > 1.45 * med_dev:
                score += 1.0
            if med_area and (area < 0.55 * med_area or area > 1.7 * med_area):
                score += 0.7
            if score >= 1.0:
                cand.append({
                    "sample_index": int(k),
                    "instance": int(with_pixels[int(positions[k])]["instance"]),
                    "heuristic_score": round(score, 2),
                })
        all_candidates.append({"series": idx, "uid": uid, "candidates": cand})

        cols = 6
        rows = int(np.ceil(len(thumbs) / cols))
        sheet = Image.new("L", (cols * 320, rows * 320), color=0)
        for j, thumb in enumerate(thumbs):
            sheet.paste(thumb, ((j % cols) * 320, (j // cols) * 320))
        img_name = f"series_{idx:03d}.jpg"
        sheet.save(img_dir / img_name, quality=90)

        summaries.append({
            "series_index": idx,
            "series_uid": uid,
            "series_number": items[0]["series_number"],
            "series_description": items[0]["series_desc"],
            "study_description": items[0]["study_desc"],
            "modality": items[0]["modality"],
            "body_part": items[0]["body_part"],
            "instances": len(items),
            "rows": items[0]["rows"],
            "cols": items[0]["cols"],
            "preview": f"/jobs/{job_dir.name}/images/{img_name}",
            "heuristic_candidates": cand,
        })

    return {
        "study": {
            "modality": next((r["modality"] for r in records if r["modality"]), ""),
            "body_part": next((r["body_part"] for r in records if r["body_part"]), ""),
            "study_description": next((r["study_desc"] for r in records if r["study_desc"]), ""),
            "total_dicom_objects": len(records),
            "series_count": len(summaries),
        },
        "series": summaries,
        "candidate_note": "Кандидаты сформированы только статистическими эвристиками для выбора срезов на дополнительный просмотр; это не диагностический вывод.",
        "candidate_count": sum(len(x["candidates"]) for x in all_candidates),
    }


JOBS = {}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(f"""
    <html><head><meta charset='utf-8'><title>DICOM Analyzer</title></head>
    <body style='font-family:Arial;max-width:1000px;margin:30px auto'>
    <h1>DICOM Analyzer</h1>
    <p>Источник по умолчанию: {'задан' if SOURCE_URL else 'не задан'}</p>
    <p><a href='/analyze'>Запустить анализ источника</a></p>
    <p><a href='/report/latest'>Последний отчёт</a></p>
    </body></html>""")


@app.get("/analyze")
def analyze(source_url: str | None = None):
    url = source_url or SOURCE_URL
    if not url:
        raise HTTPException(400, "DICOM_SOURCE_URL is not configured")
    job_id = Path(tempfile.mkdtemp(prefix="job_", dir=WORK_ROOT)).name
    job_dir = WORK_ROOT / job_id
    zip_path = job_dir / "source.zip"
    try:
        download_drive(url, zip_path)
        report = analyze_zip(zip_path, job_dir)
        report["job_id"] = job_id
        report["source"] = "Google Drive"
        (job_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        JOBS["latest"] = job_id
        return JSONResponse(report)
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, str(e))


@app.get("/report/latest", response_class=HTMLResponse)
def latest_report():
    job_id = JOBS.get("latest")
    if not job_id:
        return HTMLResponse("<h2>Анализ ещё не запускался.</h2>", status_code=404)
    return report_html(job_id)


@app.get("/report/{job_id}", response_class=HTMLResponse)
def report(job_id: str):
    return report_html(job_id)


def report_html(job_id: str) -> str:
    p = WORK_ROOT / job_id / "report.json"
    if not p.exists():
        raise HTTPException(404, "Job not found")
    data = json.loads(p.read_text(encoding="utf-8"))
    cards = []
    for s in data["series"]:
        cand = s.get("heuristic_candidates") or []
        cards.append(f"<div style='margin:20px 0'><h3>Серия {s['series_index']}: {s.get('series_description','')} ({s['instances']} срезов)</h3>"
                     f"<p>Модальность: {s.get('modality','')}; область: {s.get('body_part','')}</p>"
                     f"<img src='{s['preview']}' style='max-width:100%;border:1px solid #ccc'>"
                     f"<p>Эвристические кандидаты: {json.dumps(cand, ensure_ascii=False)}</p></div>")
    return f"<html><head><meta charset='utf-8'><title>DICOM report</title></head><body style='font-family:Arial;max-width:1200px;margin:20px auto'>"
           f"<h1>DICOM отчёт</h1><pre>{json.dumps(data['study'],ensure_ascii=False,indent=2)}</pre>"
           f"<p>{data['candidate_note']}</p>{''.join(cards)}</body></html>"


@app.get("/jobs/{job_id}/images/{name}")
def image(job_id: str, name: str):
    path = WORK_ROOT / job_id / "images" / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)
