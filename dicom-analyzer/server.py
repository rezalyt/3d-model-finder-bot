import os, re, json, shutil, tempfile, zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import numpy as np, pydicom, requests
from PIL import Image, ImageOps
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

app = FastAPI(title='DICOM Analyzer')
ROOT = Path(os.getenv('WORK_ROOT','/tmp/dicom-analyzer')); ROOT.mkdir(parents=True, exist_ok=True)
SOURCE = os.getenv('DICOM_SOURCE_URL','')
LATEST = ROOT / 'latest.txt'

def file_id(url):
    m=re.search(r'/file/d/([A-Za-z0-9_-]+)',url or '')
    if m: return m.group(1)
    return parse_qs(urlparse(url or '').query).get('id',[None])[0]

def download(url,out):
    fid=file_id(url); urls=[]
    if fid: urls += [f'https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t',f'https://drive.google.com/uc?export=download&id={fid}&confirm=t']
    urls += [url]
    s=requests.Session(); last='unknown error'
    for u in urls:
        try:
            r=s.get(u,stream=True,timeout=(20,180),allow_redirects=True,headers={'User-Agent':'Mozilla/5.0'}); r.raise_for_status()
            c=(r.headers.get('content-type') or '').lower()
            if 'text/html' in c:
                t=r.content.decode('utf-8','ignore'); m=re.search(r'confirm=([0-9A-Za-z_-]+)',t)
                if m and fid:
                    r=s.get(f'https://drive.usercontent.google.com/download?id={fid}&export=download&confirm={m.group(1)}',stream=True,timeout=(20,180),allow_redirects=True,headers={'User-Agent':'Mozilla/5.0'}); r.raise_for_status(); c=(r.headers.get('content-type') or '').lower()
            if 'text/html' in c: last='Google Drive returned an access/confirmation page'; continue
            with open(out,'wb') as f:
                for ch in r.iter_content(8*1024*1024):
                    if ch: f.write(ch)
            if Path(out).stat().st_size: return
        except Exception as e: last=str(e)
    raise RuntimeError(last)

def arr(ds):
    try:
        a=ds.pixel_array.astype('float32'); a=a*float(getattr(ds,'RescaleSlope',1))+float(getattr(ds,'RescaleIntercept',0))
        if a.ndim>2: a=a[0]
        return np.nan_to_num(a)
    except Exception: return None

def thumb(a):
    lo,hi=np.percentile(a,[1,99]); hi=max(hi,lo+1); a=np.clip((a-lo)/(hi-lo),0,1)
    return ImageOps.fit(Image.fromarray((a*255).astype('uint8'),'L'),(280,280),method=Image.Resampling.BILINEAR)

def run(url):
    job=Path(tempfile.mkdtemp(prefix='job_',dir=ROOT)); z=job/'src.zip'; extract=job/'dicom'; imgs=job/'images'; imgs.mkdir()
    download(url,z)
    with zipfile.ZipFile(z) as zz:
        total=0
        for m in zz.infolist():
            if m.is_dir() or Path(m.filename).is_absolute() or '..' in Path(m.filename).parts: continue
            total += m.file_size
            if total>25*1024**3: raise RuntimeError('Archive exceeds 25 GB limit')
        zz.extractall(extract)
    rec=[]
    for p in extract.rglob('*'):
        if not p.is_file(): continue
        try:
            d=pydicom.dcmread(str(p),force=True); a=arr(d)
            if a is None: continue
            rec.append((str(getattr(d,'SeriesInstanceUID','unknown')),int(getattr(d,'InstanceNumber',0) or 0),d,a))
        except Exception: pass
    if not rec: raise RuntimeError('No readable DICOM images found')
    groups={}
    for r in rec: groups.setdefault(r[0],[]).append(r)
    series=[]
    for i,(uid,rs) in enumerate(groups.items(),1):
        rs.sort(key=lambda x:(x[1],x[0])); take=min(36,len(rs)); pos=np.linspace(0,len(rs)-1,take).round().astype(int); ims=[]; scores=[]
        for j in pos:
            a=rs[int(j)][3]; ims.append(thumb(a)); lo,hi=np.percentile(a,[5,95]); scores.append(float(np.std(np.clip(a,lo,hi))))
        med=float(np.median(scores)); cand=[{'sample_index':k,'instance':rs[int(pos[k])][1],'score':round(scores[k]/med,2)} for k in range(len(scores)) if med and scores[k]>1.5*med]
        cols=6; rows=int(np.ceil(len(ims)/cols)); sheet=Image.new('L',(cols*280,rows*280),0)
        for k,im in enumerate(ims): sheet.paste(im,((k%cols)*280,(k//cols)*280))
        name=f'series_{i:03d}.jpg'; sheet.save(imgs/name,quality=88)
        d=rs[0][2]
        series.append({'series':i,'instances':len(rs),'modality':str(getattr(d,'Modality','')),'body_part':str(getattr(d,'BodyPartExamined','')),'description':str(getattr(d,'SeriesDescription','')),'rows':int(getattr(d,'Rows',0) or 0),'cols':int(getattr(d,'Columns',0) or 0),'preview':f'/jobs/{job.name}/{name}','candidates':cand})
    report={'job_id':job.name,'study':{'objects':len(rec),'series':len(series),'modality':series[0]['modality'],'body_part':series[0]['body_part']},'series':series,'note':'Кандидаты — статистические срезы для дополнительного просмотра; это не диагноз.'}
    (job/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8'); LATEST.write_text(job.name)
    return report

@app.get('/health')
def health(): return {'ok':True}

@app.get('/')
def index(): return HTMLResponse('<h1>DICOM Analyzer</h1><p><a href="/analyze">Запустить анализ</a></p><p><a href="/report/latest">Последний отчёт</a></p>')

@app.get('/analyze')
def analyze(url:str|None=None):
    src=url or SOURCE
    if not src: raise HTTPException(400,'DICOM_SOURCE_URL is not configured')
    try: return run(src)
    except Exception as e: raise HTTPException(500,str(e))

@app.get('/report/latest',response_class=HTMLResponse)
def latest():
    if not LATEST.exists(): raise HTTPException(404,'No report')
    return report(LATEST.read_text().strip())

@app.get('/report/{job}',response_class=HTMLResponse)
def report(job):
    p=ROOT/job/'report.json'
    if not p.exists(): raise HTTPException(404,'No report')
    d=json.loads(p.read_text()); blocks=[]
    for s in d['series']:
        blocks.append(f"<h2>Серия {s['series']}: {s['description']}</h2><p>{s['instances']} срезов; modality={s['modality']}; body={s['body_part']}</p><img style='max-width:100%' src='{s['preview']}'><p>Кандидаты: {s['candidates']}</p>")
    return HTMLResponse('<html><meta charset="utf-8"><body style="font-family:Arial;max-width:1200px;margin:20px auto"><h1>DICOM отчёт</h1><pre>'+json.dumps(d['study'],ensure_ascii=False,indent=2)+'</pre><p>'+d['note']+'</p>'+''.join(blocks)+'</body></html>')

@app.get('/jobs/{job}/{name}')
def image(job,name):
    p=ROOT/job/'images'/Path(name).name
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p)
