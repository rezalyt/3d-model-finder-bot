import io, json, os, re, threading, uuid, zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pydicom, requests
from PIL import Image, ImageOps
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

app = FastAPI(title="DICOM Analyzer", version="0.2.0")
ROOT = Path(os.getenv("WORK_ROOT", "/tmp/dicom-analyzer")); ROOT.mkdir(parents=True, exist_ok=True)
SOURCE = os.getenv("DICOM_SOURCE_URL", "")
JOBS = {}; LOCK = threading.Lock()

def file_id(url):
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", url or "")
    if m: return m.group(1)
    return parse_qs(urlparse(url or "").query).get("id", [None])[0]

def download(url, out):
    fid = file_id(url); candidates=[]
    if fid:
        candidates += [f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t", f"https://drive.google.com/uc?export=download&id={fid}&confirm=t"]
    candidates.append(url); last="unknown error"; s=requests.Session()
    for u in candidates:
        try:
            r=s.get(u,stream=True,timeout=(20,300),allow_redirects=True,headers={"User-Agent":"Mozilla/5.0"}); r.raise_for_status(); c=(r.headers.get("content-type") or "").lower()
            if "text/html" in c:
                txt=r.content.decode("utf-8","ignore"); m=re.search(r"confirm=([0-9A-Za-z_-]+)",txt)
                if m and fid:
                    r=s.get(f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm={m.group(1)}",stream=True,timeout=(20,300),allow_redirects=True,headers={"User-Agent":"Mozilla/5.0"}); r.raise_for_status(); c=(r.headers.get("content-type") or "").lower()
                if "text/html" in c: last="Google Drive returned an HTML access/confirmation page"; continue
            with open(out,"wb") as f:
                for ch in r.iter_content(8*1024*1024):
                    if ch: f.write(ch)
            if Path(out).stat().st_size>0: return
        except Exception as e: last=str(e)
    raise RuntimeError(last)

def read_ds(data,pixels=False): return pydicom.dcmread(io.BytesIO(data),stop_before_pixels=not pixels,force=True)

def make_preview(a):
    a=np.nan_to_num(a.astype(np.float32)); lo,hi=np.percentile(a,[1,99]) if a.size else (0,1); hi=max(hi,lo+1); a=np.clip((a-lo)/(hi-lo),0,1)
    return ImageOps.fit(Image.fromarray((a*255).astype(np.uint8),mode="L"),(320,320),method=Image.Resampling.BILINEAR)

def process(job_id,url):
    job=ROOT/job_id; job.mkdir(parents=True,exist_ok=True); zpath=job/"source.zip"; idir=job/"images"; idir.mkdir(exist_ok=True)
    with LOCK: JOBS[job_id]={"job_id":job_id,"status":"downloading"}
    try:
        download(url,zpath)
        with LOCK: JOBS[job_id]={"job_id":job_id,"status":"reading_metadata"}
        series={}
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                if info.is_dir(): continue
                p=Path(info.filename)
                if p.is_absolute() or ".." in p.parts: continue
                try:
                    with z.open(info) as fh: data=fh.read()
                    ds=read_ds(data,False)
                    if not hasattr(ds,"PixelData"): continue
                    uid=str(getattr(ds,"SeriesInstanceUID","unknown"))
                    series.setdefault(uid,[]).append({"name":info.filename,"instance":int(getattr(ds,"InstanceNumber",0) or 0),"series_number":int(getattr(ds,"SeriesNumber",0) or 0),"modality":str(getattr(ds,"Modality","")),"body_part":str(getattr(ds,"BodyPartExamined","")),"description":str(getattr(ds,"SeriesDescription","")),"rows":int(getattr(ds,"Rows",0) or 0),"cols":int(getattr(ds,"Columns",0) or 0)})
                    del data,ds
                except Exception: continue
        if not series: raise RuntimeError("No readable DICOM image objects found")
        summaries=[]
        with zipfile.ZipFile(zpath) as z:
            for idx,(uid,items) in enumerate(series.items(),1):
                items.sort(key=lambda x:(x["instance"],x["name"])); take=min(18,len(items)); positions=np.linspace(0,len(items)-1,take).round().astype(int); thumbs=[]; scores=[]
                for pos in positions:
                    try:
                        with z.open(items[int(pos)]["name"]) as fh: data=fh.read()
                        ds=read_ds(data,True); a=ds.pixel_array.astype(np.float32); a=a*float(getattr(ds,"RescaleSlope",1.0))+float(getattr(ds,"RescaleIntercept",0.0))
                        if a.ndim>2: a=a[0]
                        thumbs.append(make_preview(a)); p1,p99=np.percentile(a,[5,95]); scores.append(float(np.std(np.clip(a,p1,p99))))
                        del data,ds,a
                    except Exception: continue
                med=float(np.median(scores)) if scores else 0; candidates=[]
                if med:
                    for k,s in enumerate(scores):
                        if s>1.5*med: candidates.append({"sample_index":k,"instance":items[int(positions[k])]["instance"],"heuristic_score":round(s/med,2)})
                prev=None
                if thumbs:
                    cols=6; rows=int(np.ceil(len(thumbs)/cols)); sheet=Image.new("L",(cols*320,rows*320),0)
                    for k,img in enumerate(thumbs): sheet.paste(img,((k%cols)*320,(k//cols)*320))
                    name=f"series_{idx:03d}.jpg"; sheet.save(idir/name,quality=88); prev=f"/jobs/{job_id}/images/{name}"
                base=items[0]; summaries.append({"series_index":idx,"series_uid":uid,"series_number":base["series_number"],"series_description":base["description"],"modality":base["modality"],"body_part":base["body_part"],"instances":len(items),"rows":base["rows"],"cols":base["cols"],"preview":prev,"heuristic_candidates":candidates})
        report={"job_id":job_id,"source":"Google Drive","study":{"total_series":len(summaries),"total_dicom_objects":sum(len(v) for v in series.values()),"modality":summaries[0]["modality"] if summaries else "","body_part":summaries[0]["body_part"] if summaries else ""},"series":summaries,"note":"Эвристические кандидаты предназначены для дополнительного просмотра и не являются диагнозом."}
        (job/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        with LOCK: JOBS[job_id]={"job_id":job_id,"status":"completed"}
    except Exception as e:
        (job/"error.txt").write_text(str(e),encoding="utf-8")
        with LOCK: JOBS[job_id]={"job_id":job_id,"status":"failed","error":str(e)}
    finally:
        try: zpath.unlink(missing_ok=True)
        except Exception: pass

@app.get("/health")
def health(): return {"ok":True,"service":"dicom-analyzer","jobs":len(JOBS)}
@app.get("/")
def index(): return HTMLResponse("<h1>DICOM Analyzer</h1><p><a href='/analyze'>Start analysis</a></p>")
@app.get("/analyze")
def analyze(url:str|None=None):
    src=url or SOURCE
    if not src: raise HTTPException(400,"DICOM_SOURCE_URL is not configured")
    job_id=uuid.uuid4().hex[:12]
    with LOCK: JOBS[job_id]={"job_id":job_id,"status":"queued"}
    threading.Thread(target=process,args=(job_id,src),daemon=True).start()
    return JSONResponse({"job_id":job_id,"status_url":f"/status/{job_id}","report_url":f"/report/{job_id}","message":"Analysis started"},status_code=202)
@app.get("/status/{job_id}")
def status(job_id):
    with LOCK: info=JOBS.get(job_id)
    if info: return info
    job=ROOT/job_id
    if (job/"report.json").exists(): return {"job_id":job_id,"status":"completed"}
    if (job/"error.txt").exists(): return {"job_id":job_id,"status":"failed","error":(job/"error.txt").read_text(encoding="utf-8")}
    raise HTTPException(404,"Unknown job")
@app.get("/report/{job_id}",response_class=HTMLResponse)
def report(job_id):
    p=ROOT/job_id/"report.json"
    if not p.exists(): raise HTTPException(404,"Report is not ready")
    d=json.loads(p.read_text(encoding="utf-8")); blocks=[]
    for s in d["series"]:
        img=f"<img src='{s['preview']}' style='max-width:100%;border:1px solid #ccc'>" if s.get("preview") else ""; blocks.append(f"<h2>Серия {s['series_index']}: {s.get('series_description','')}</h2><p>{s['instances']} срезов; {s.get('modality','')} / {s.get('body_part','')}</p>{img}<p>Кандидаты: {json.dumps(s.get('heuristic_candidates',[]),ensure_ascii=False)}</p>")
    return HTMLResponse("<html><meta charset='utf-8'><body style='font-family:Arial;max-width:1200px;margin:20px auto'><h1>DICOM отчёт</h1><pre>"+json.dumps(d['study'],ensure_ascii=False,indent=2)+"</pre><p>"+d['note']+"</p>"+''.join(blocks)+"</body></html>")
@app.get("/jobs/{job_id}/images/{name}")
def image(job_id,name):
    p=ROOT/job_id/"images"/Path(name).name
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p)
