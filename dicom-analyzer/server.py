import io, json, os, re, subprocess, threading, uuid, zipfile, shutil
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pydicom, requests, dicom2nifti
from PIL import Image, ImageOps
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

app = FastAPI(title="DICOM Analyzer", version="0.4.1")
ROOT = Path(os.getenv("WORK_ROOT", "/tmp/dicom-analyzer")); ROOT.mkdir(parents=True, exist_ok=True)
SOURCE = os.getenv("DICOM_SOURCE_URL", "")
JOBS = {}; LOCK = threading.Lock()
THUMB_SIZE = 256
AI_ROIS = ["liver", "pancreas", "gallbladder", "spleen", "kidney_left", "kidney_right", "adrenal_gland_left", "adrenal_gland_right", "aorta"]


def file_id(url):
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", url or "")
    if m: return m.group(1)
    return parse_qs(urlparse(url or "").query).get("id", [None])[0]


def download(url, out):
    fid = file_id(url); candidates = []
    if fid:
        candidates += [f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t", f"https://drive.google.com/uc?export=download&id={fid}&confirm=t"]
    candidates.append(url); last = "unknown error"; s = requests.Session()
    for u in candidates:
        try:
            r = s.get(u, stream=True, timeout=(20, 300), allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}); r.raise_for_status()
            c = (r.headers.get("content-type") or "").lower()
            if "text/html" in c:
                txt = r.content.decode("utf-8", "ignore"); m = re.search(r"confirm=([0-9A-Za-z_-]+)", txt)
                if m and fid:
                    r = s.get(f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm={m.group(1)}", stream=True, timeout=(20, 300), allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}); r.raise_for_status(); c = (r.headers.get("content-type") or "").lower()
                if "text/html" in c: last = "Google Drive returned an HTML access/confirmation page"; continue
            with open(out, "wb") as f:
                for ch in r.iter_content(8 * 1024 * 1024):
                    if ch: f.write(ch)
            if Path(out).stat().st_size > 0: return
        except Exception as e: last = str(e)
    raise RuntimeError(last)


def read_ds(data, pixels=False): return pydicom.dcmread(io.BytesIO(data), stop_before_pixels=not pixels, force=True)

def to_hu(ds):
    a = ds.pixel_array.astype(np.float32); a = a * float(getattr(ds, "RescaleSlope", 1.0)) + float(getattr(ds, "RescaleIntercept", 0.0))
    if a.ndim > 2: a = a[0]
    return np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)

def window(a, center, width):
    lo, hi = center - width / 2.0, center + width / 2.0; x = np.clip((a - lo) / max(width, 1.0), 0, 1)
    return Image.fromarray((x * 255).astype(np.uint8), mode="L")

def panel(a):
    imgs = [window(a, 40, 350), window(a, -600, 1500), window(a, 300, 2500)]
    imgs = [ImageOps.fit(x, (THUMB_SIZE, THUMB_SIZE), method=Image.Resampling.BILINEAR) for x in imgs]
    out = Image.new("RGB", (THUMB_SIZE * 3, THUMB_SIZE))
    for i, img in enumerate(imgs): out.paste(img.convert("RGB"), (THUMB_SIZE * i, 0))
    return out


def read_series_inventory(zpath):
    series = {}
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir(): continue
            p = Path(info.filename)
            if p.is_absolute() or ".." in p.parts: continue
            try:
                with z.open(info) as fh: data = fh.read()
                ds = read_ds(data, False)
                is_image = hasattr(ds, "SOPClassUID") and hasattr(ds, "Rows") and hasattr(ds, "Columns")
                if not is_image: continue
                uid = str(getattr(ds, "SeriesInstanceUID", "unknown"))
                series.setdefault(uid, []).append({"name": info.filename, "instance": int(getattr(ds, "InstanceNumber", 0) or 0), "series_number": int(getattr(ds, "SeriesNumber", 0) or 0), "modality": str(getattr(ds, "Modality", "")), "body_part": str(getattr(ds, "BodyPartExamined", "")), "description": str(getattr(ds, "SeriesDescription", "")), "rows": int(getattr(ds, "Rows", 0) or 0), "cols": int(getattr(ds, "Columns", 0) or 0)})
            except Exception: continue
    for items in series.values(): items.sort(key=lambda x: (x["instance"], x["name"]))
    return series


def process_full(job_id, url):
    job = ROOT / job_id; job.mkdir(parents=True, exist_ok=True); zpath = job / "source.zip"; idir = job / "images"; idir.mkdir(exist_ok=True)
    with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "downloading", "processed_slices": 0}
    try:
        download(url, zpath)
        with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "reading_metadata", "processed_slices": 0}
        series = read_series_inventory(zpath)
        if not series: raise RuntimeError("No readable DICOM image objects found")
        summaries = []; total_processed = 0
        with zipfile.ZipFile(zpath) as z:
            for idx, (uid, items) in enumerate(series.items(), 1):
                sdir = idir / f"series_{idx:03d}"; sdir.mkdir(exist_ok=True); slice_rows = []; scores = []
                for n, item in enumerate(items, 1):
                    try:
                        with z.open(item["name"]) as fh: data = fh.read()
                        ds = read_ds(data, True); a = to_hu(ds)
                        if a.size == 0: continue
                        p1, p99 = np.percentile(a, [5, 95]); scores.append(float(np.std(np.clip(a, p1, p99))) )
                        out = panel(a); fname = f"slice_{n:05d}.jpg"; out.save(sdir / fname, quality=72, optimize=True)
                        slice_rows.append({"index": n, "instance": item["instance"], "file": f"/jobs/{job_id}/images/series_{idx:03d}/{fname}"}); total_processed += 1
                        if total_processed % 10 == 0: 
                            with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "processing_all_slices", "processed_slices": total_processed}
                        del data, ds, a, out
                    except Exception: continue
                med = float(np.median(scores)) if scores else 0.0; candidates = []
                if med:
                    scored = [(s / med, r) for r, s in zip(slice_rows, scores)]
                    for ratio, r in sorted(scored, reverse=True)[:20]: candidates.append({"index": r["index"], "instance": r["instance"], "outlier_ratio": round(float(ratio), 2)})
                base = items[0]
                summaries.append({"series_index": idx, "series_uid": uid, "series_number": base["series_number"], "series_description": base["description"], "modality": base["modality"], "body_part": base["body_part"], "instances": len(items), "processed_slices": len(slice_rows), "rows": base["rows"], "cols": base["cols"], "viewer": f"/viewer/{job_id}/{idx}", "top_candidates": candidates, "slices": slice_rows})
        report = {"job_id": job_id, "source": "Google Drive", "analysis": {"coverage": "all_readable_slices", "ai_model": None, "note": "Все читаемые срезы обработаны автоматически. Количественные кандидаты не являются диагнозом."}, "study": {"total_series": len(summaries), "total_dicom_objects": sum(len(v) for v in series.values()), "total_processed_slices": total_processed, "modality": summaries[0]["modality"] if summaries else "", "body_part": summaries[0]["body_part"] if summaries else ""}, "series": summaries}
        (job / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "completed", "processed_slices": total_processed}
    except Exception as e:
        (job / "error.txt").write_text(str(e), encoding="utf-8"); with_lock = {"job_id": job_id, "status": "failed", "error": str(e)}
        with LOCK: JOBS[job_id] = with_lock
    finally:
        try: zpath.unlink(missing_ok=True)
        except Exception: pass


def process_ai(job_id, url):
    job = ROOT / job_id; job.mkdir(parents=True, exist_ok=True); zpath = job / "source.zip"; work = job / "ai_work"; dicom_dir = work / "dicom"; nifti_dir = work / "nifti"; out = job / "totalsegmentator"
    with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "ai_downloading", "model": "TotalSegmentator 2.18.0"}
    try:
        download(url, zpath); work.mkdir(exist_ok=True); dicom_dir.mkdir(exist_ok=True); nifti_dir.mkdir(exist_ok=True); out.mkdir(exist_ok=True)
        series = read_series_inventory(zpath); eligible = []
        skip_words = ("topogram", "scout", "localizer", "monitor", "premonitor")
        for uid, items in series.items():
            if len(items) < 25: continue
            desc = (items[0]["description"] or "").lower()
            if any(w in desc for w in skip_words): continue
            if items[0]["modality"] != "CT": continue
            eligible.append((len(items), uid, items))
        if not eligible: raise RuntimeError("No suitable diagnostic CT series for AI segmentation")
        eligible.sort(reverse=True)
        _, uid, items = eligible[0]
        with zipfile.ZipFile(zpath) as z:
            for item in items:
                target = dicom_dir / Path(item["name"]).name
                with z.open(item["name"]) as src, open(target, "wb") as dst: shutil.copyfileobj(src, dst, length=1024*1024)
        with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "ai_converting_to_nifti", "model": "TotalSegmentator 2.18.0", "series_instances": len(items), "series_description": items[0]["description"]}
        dicom2nifti.convert_directory(str(dicom_dir), str(nifti_dir), compression=True, reorient=True)
        nifti_files = sorted(nifti_dir.glob("*.nii.gz"))
        if not nifti_files: raise RuntimeError("DICOM-to-NIfTI conversion produced no NIfTI volume")
        input_nii = nifti_files[0]
        cmd = ["TotalSegmentator", "-i", str(input_nii), "-o", str(out), "--fast", "--device", "cpu", "--roi_subset", *AI_ROIS, "--statistics", "--quiet"]
        with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "ai_segmenting", "model": "TotalSegmentator 2.18.0", "device": "cpu", "series_instances": len(items), "series_description": items[0]["description"], "rois": AI_ROIS}
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=3600)
        (job / "ai_stdout.log").write_text(proc.stdout or "", encoding="utf-8")
        if proc.returncode != 0: raise RuntimeError("TotalSegmentator failed: " + (proc.stdout or "")[-6000:])
        stats_path = out / "statistics.json"; stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}
        result = {"job_id": job_id, "status": "completed", "model": "TotalSegmentator 2.18.0", "device": "cpu", "mode": "fast", "series_description": items[0]["description"], "series_instances": len(items), "rois": AI_ROIS, "statistics": stats}
        (job / "ai_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with LOCK: JOBS[job_id] = result
    except Exception as e:
        (job / "ai_error.txt").write_text(str(e), encoding="utf-8")
        with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "failed", "error": str(e), "model": "TotalSegmentator 2.18.0"}
    finally:
        try: shutil.rmtree(work, ignore_errors=True); zpath.unlink(missing_ok=True)
        except Exception: pass


@app.get("/health")
def health(): return {"ok": True, "service": "dicom-analyzer", "jobs": len(JOBS), "ai_model": "TotalSegmentator 2.18.0"}

@app.get("/")
def index(): return HTMLResponse("<h1>DICOM Analyzer</h1><p><a href='/analyze'>Full DICOM scan</a></p><p><a href='/analyze-ai'>Focused AI segmentation</a></p>")

@app.get("/analyze")
def analyze(url: str | None = None):
    src = url or SOURCE
    if not src: raise HTTPException(400, "DICOM_SOURCE_URL is not configured")
    job_id = uuid.uuid4().hex[:12]
    with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "queued"}
    threading.Thread(target=process_full, args=(job_id, src), daemon=True).start()
    return JSONResponse({"job_id": job_id, "status_url": f"/status/{job_id}", "report_url": f"/report/{job_id}", "message": "Full-slice analysis started"}, status_code=202)

@app.get("/analyze-ai")
def analyze_ai(url: str | None = None):
    src = url or SOURCE
    if not src: raise HTTPException(400, "DICOM_SOURCE_URL is not configured")
    job_id = uuid.uuid4().hex[:12]
    with LOCK: JOBS[job_id] = {"job_id": job_id, "status": "queued", "model": "TotalSegmentator 2.18.0"}
    threading.Thread(target=process_ai, args=(job_id, src), daemon=True).start()
    return JSONResponse({"job_id": job_id, "status_url": f"/status/{job_id}", "ai_report_url": f"/ai-report/{job_id}", "message": "Focused AI segmentation started"}, status_code=202)

@app.get("/status/{job_id}")
def status(job_id):
    with LOCK: info = JOBS.get(job_id)
    if info: return info
    job = ROOT / job_id
    if (job / "report.json").exists(): return {"job_id": job_id, "status": "completed"}
    if (job / "ai_report.json").exists(): return json.loads((job / "ai_report.json").read_text(encoding="utf-8"))
    if (job / "error.txt").exists(): return {"job_id": job_id, "status": "failed", "error": (job / "error.txt").read_text(encoding="utf-8")}
    if (job / "ai_error.txt").exists(): return {"job_id": job_id, "status": "failed", "error": (job / "ai_error.txt").read_text(encoding="utf-8")}
    raise HTTPException(404, "Unknown job")

@app.get("/ai-report/{job_id}", response_class=HTMLResponse)
def ai_report(job_id):
    p = ROOT / job_id / "ai_report.json"
    if not p.exists(): raise HTTPException(404, "AI report is not ready")
    d = json.loads(p.read_text(encoding="utf-8")); return HTMLResponse("<html><meta charset='utf-8'><body style='font-family:Arial;max-width:1000px;margin:20px auto'><h1>AI-анализ КТ</h1><p>Модель: " + d["model"] + " | Режим: " + d["mode"] + " | CPU</p><p>Серия: " + str(d.get("series_description","")) + " (" + str(d.get("series_instances","")) + " срезов)</p><p>Сегментация: " + ", ".join(d["rois"]) + "</p><pre>" + json.dumps(d["statistics"], ensure_ascii=False, indent=2) + "</pre><p>Результат не является медицинским диагнозом.</p></body></html>")

@app.get("/report/{job_id}", response_class=HTMLResponse)
def report(job_id):
    p = ROOT / job_id / "report.json"
    if not p.exists(): raise HTTPException(404, "Report is not ready")
    d = json.loads(p.read_text(encoding="utf-8")); blocks=[]
    for s in d["series"]: blocks.append(f"<section><h2>Серия {s['series_index']}: {s.get('series_description','')}</h2><p>{s['instances']} исходных / {s['processed_slices']} обработано; {s.get('modality','')} / {s.get('body_part','')}</p><p><a href='{s['viewer']}'>Открыть все срезы</a></p><p>Топ-кандидаты: {json.dumps(s.get('top_candidates',[]),ensure_ascii=False)}</p></section>")
    return HTMLResponse("<html><meta charset='utf-8'><body style='font-family:Arial;max-width:1200px;margin:20px auto'><h1>DICOM отчёт</h1><pre>" + json.dumps(d['study'],ensure_ascii=False,indent=2) + "</pre><p>" + d['analysis']['note'] + "</p>" + ''.join(blocks) + "</body></html>")

@app.get("/viewer/{job_id}/{series_index}", response_class=HTMLResponse)
def viewer(job_id, series_index: int):
    p = ROOT / job_id / "report.json"
    if not p.exists(): raise HTTPException(404, "Report is not ready")
    d = json.loads(p.read_text(encoding="utf-8")); s = next((x for x in d["series"] if x["series_index"] == series_index), None)
    if not s or not s["slices"]: raise HTTPException(404, "Series not found")
    payload = json.dumps(s["slices"], ensure_ascii=False); first = s["slices"][0]["file"]
    html = f"<html><meta charset='utf-8'><style>body{{font-family:Arial;max-width:1100px;margin:20px auto}}#img{{max-width:100%;background:#111}}input{{width:100%}}</style><h1>Серия {series_index}: {s.get('series_description','')}</h1><p>Срез <span id='n'>1</span> из {len(s['slices'])}</p><input id='r' type='range' min='1' max='{len(s['slices'])}' value='1'><img id='img' src='{first}'><pre id='meta'></pre><script>const a={payload};const r=document.getElementById('r'),img=document.getElementById('img'),n=document.getElementById('n'),m=document.getElementById('meta');function go(){{let x=a[+r.value-1];n.textContent=x.index;img.src=x.file;m.textContent=JSON.stringify(x,null,2)}}r.oninput=go;go();</script></html>"
    return HTMLResponse(html)

@app.get("/jobs/{job_id}/images/{series}/{name}")
def image(job_id, series, name):
    p = ROOT / job_id / "images" / Path(series).name / Path(name).name
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p)
