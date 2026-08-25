#!/usr/bin/env python3
"""Detailed research review for all screening candidates.

Consumes screening_v2.json, reconstructs selected CT phases, creates axial
neighbour-slice overlays for every candidate, and records DICOM navigation
metadata when recoverable. Research/review only; not a medical diagnosis.
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
from PIL import Image, ImageDraw
import dicom2nifti

PHASES = ("native", "arterial", "portal", "delayed")
SKIP = ("topogram", "scout", "localizer", "monitor", "premonitor")

def py(v):
    if isinstance(v, np.generic): return v.item()
    if isinstance(v, np.ndarray): return v.tolist()
    if isinstance(v, dict): return {k: py(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)): return [py(x) for x in v]
    return v

def dump_json(path, data):
    path.write_text(json.dumps(py(data), ensure_ascii=False, indent=2, default=lambda o: o.item() if hasattr(o, 'item') else str(o)), encoding='utf-8')

def inventory(zip_path):
    series = {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir(): continue
            p = Path(info.filename)
            if p.is_absolute() or '..' in p.parts: continue
            try:
                with z.open(info) as fh:
                    ds = pydicom.dcmread(fh, stop_before_pixels=True, force=True)
                if getattr(ds, 'Modality', '') != 'CT' or not hasattr(ds, 'Rows'): continue
                uid = str(getattr(ds, 'SeriesInstanceUID', 'unknown'))
                series.setdefault(uid, []).append({
                    'name': info.filename,
                    'instance': int(getattr(ds, 'InstanceNumber', 0) or 0),
                    'description': str(getattr(ds, 'SeriesDescription', '')),
                    'sop_uid': str(getattr(ds, 'SOPInstanceUID', '')),
                })
            except Exception:
                continue
    for items in series.values(): items.sort(key=lambda x: (x['instance'], x['name']))
    return series

def classify_phase(desc):
    d = desc.lower().replace('_', ' ')
    rules = {'native': ('non contrast','non-contrast','noncontrast','native','without contrast'), 'arterial': ('arterial',), 'portal': ('portal','venous','porto'), 'delayed': ('delayed',)}
    for phase, words in rules.items():
        if any(w in d for w in words): return phase
    return 'other'

def copy_series(zip_path, items, dst):
    dst.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for n, item in enumerate(items, 1):
            with z.open(item['name']) as src, open(dst / f'slice_{n:05d}.dcm', 'wb') as out:
                shutil.copyfileobj(src, out, 4 * 1024 * 1024)

def reconstruct(zip_path, items, tmp):
    dcm, nii = tmp/'dicom', tmp/'nifti'
    copy_series(zip_path, items, dcm); nii.mkdir(parents=True, exist_ok=True)
    dicom2nifti.convert_directory(str(dcm), str(nii), compression=True, reorient=True)
    files = sorted(nii.glob('*.nii.gz'))
    if not files: raise RuntimeError('DICOM-to-NIfTI conversion produced no volume')
    img = nib.load(str(files[0])); vol = img.get_fdata(dtype=np.float32)
    return img, vol

def window(arr, center=40.0, width=400.0):
    lo, hi = center-width/2.0, center+width/2.0
    return (np.clip((arr-lo)/max(hi-lo,1.0),0,1)*255).astype(np.uint8)

def crop(arr, cy, cx, size=220):
    h,w=arr.shape; half=size//2
    y0=max(0,min(max(h-size,0),int(round(cy))-half)); x0=max(0,min(max(w-size,0),int(round(cx))-half))
    return arr[y0:min(y0+size,h), x0:min(x0+size,w)], y0, x0

def nifti_to_patient(img, zyx):
    z,y,x=[float(v) for v in zyx]
    xyz=np.array([x,y,z,1.0],dtype=float)
    return (np.asarray(img.affine,dtype=float) @ xyz)[:3]

def load_dicom_nav(zip_path, items, target_z, tol=3):
    # Retrieve ImagePositionPatient/InstanceNumber around the target slice.
    rows=[]
    with zipfile.ZipFile(zip_path) as z:
        for n,item in enumerate(items,1):
            try:
                with z.open(item['name']) as fh: ds=pydicom.dcmread(fh, stop_before_pixels=True, force=True)
                ipp=getattr(ds,'ImagePositionPatient',None)
                rows.append({'index':n-1,'instance':int(getattr(ds,'InstanceNumber',0) or 0),'sop_uid':str(getattr(ds,'SOPInstanceUID','')),'image_position_patient':py(ipp) if ipp is not None else None})
            except Exception: pass
    if not rows: return None
    idx=max(0,min(len(rows)-1,int(round(target_z))))
    return rows[idx]

def make_overlay(vol, obs, img, out_path, title, delta=0):
    z,y,x=[float(v) for v in obs['centroid_zyx']]
    zi=max(0,min(vol.shape[0]-1,int(round(z+delta))))
    crop_img,y0,x0=crop(vol[zi,:,:],y,x)
    base=Image.fromarray(window(crop_img)).convert('RGB'); d=ImageDraw.Draw(base)
    px,pyc=int(round(x))-x0,int(round(y))-y0; r=max(6,min(15,int(max(base.size)/24)))
    d.ellipse((px-r,pyc-r,px+r,pyc+r),outline=(255,0,0),width=3)
    d.line((px-2*r,pyc,px+2*r,pyc),fill=(255,0,0),width=1); d.line((px,pyc-2*r,px,pyc+2*r),fill=(255,0,0),width=1)
    canvas=Image.new('RGB',(base.width,base.height+46),(25,25,25)); canvas.paste(base,(0,46)); dc=ImageDraw.Draw(canvas)
    dc.text((8,7),title,fill=(255,255,255)); dc.text((8,26),f'z={zi} HU={obs.get("mean_hu","n/a")}',fill=(220,220,220)); canvas.save(out_path)
    return zi

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('zip',type=Path); ap.add_argument('--input',type=Path,default=Path('dicom-ai-result/screening_v2.json')); ap.add_argument('--out',type=Path,default=Path('dicom-ai-result/detailed_review_v4')); ap.add_argument('--radius',type=int,default=2)
    args=ap.parse_args()
    if not args.zip.exists(): raise SystemExit(f'ZIP not found: {args.zip}')
    if not args.input.exists(): raise SystemExit(f'Input JSON not found: {args.input}')
    report=json.loads(args.input.read_text(encoding='utf-8')); candidates=report.get('candidates',[])
    if not candidates: raise SystemExit('No candidates in screening_v2.json')
    root=args.out; root.mkdir(parents=True,exist_ok=True)
    phase_map={}
    for uid,items in inventory(args.zip).items():
        if len(items)<25: continue
        desc=items[0]['description']
        if any(w in desc.lower() for w in SKIP): continue
        phase=classify_phase(desc)
        if phase=='other': continue
        if phase not in phase_map or len(items)>len(phase_map[phase]['items']): phase_map[phase]={'uid':uid,'items':items,'description':desc}
    results=[]
    for c in candidates:
        cid=str(c.get('candidate_id','candidate')); cdir=root/cid; cdir.mkdir(parents=True,exist_ok=True)
        obs_by_phase={str(o.get('phase')):o for o in c.get('observations',[])}; fallback=(c.get('observations') or [None])[0]
        overlays=[]; errors=[]
        for phase in PHASES:
            s=phase_map.get(phase)
            if not s: continue
            obs=obs_by_phase.get(phase) or fallback
            if not obs: continue
            try:
                with tempfile.TemporaryDirectory(prefix='detailed_v4_') as td:
                    img,vol=reconstruct(args.zip,s['items'],Path(td))
                    z=float(obs['centroid_zyx'][0]); base=int(round(z))
                    nav=load_dicom_nav(args.zip,s['items'],base)
                    neighbour_files=[]
                    for off in range(-args.radius,args.radius+1):
                        p=cdir/f'{phase}_{off:+d}.png'
                        zi=make_overlay(vol,obs,img,p,f'{cid} | {c.get("organ")} | {phase} | offset {off}',off)
                        neighbour_files.append({'offset':off,'path':str(p),'nifti_z':zi})
                    overlays.append({'phase':phase,'series':s['description'],'centroid_zyx':py(obs['centroid_zyx']),'patient_xyz':py(nifti_to_patient(img,obs['centroid_zyx'])),'dicom_navigation':nav,'slices':neighbour_files,'image_zoom':py(img.header.get_zooms()[:3]),'volume_shape':py(vol.shape)})
            except Exception as exc: errors.append({'phase':phase,'error':str(exc)})
        entry={'candidate_id':cid,'organ':c.get('organ'),'triage_level':c.get('triage_level'),'triage_score':c.get('triage_score'),'screening_priority':c.get('screening_priority'),'triage_reasons':c.get('triage_reasons',[]),'observations':c.get('observations',[]),'overlays':overlays,'errors':errors}
        dump_json(cdir/'candidate.json',entry); results.append(entry); print(f'[OK] {cid}: {len(overlays)} phase set(s)')
    result={'type':'research_detailed_review_v4','warning':'Research/review support only; not a medical diagnosis.','summary':{'input_candidates':len(candidates),'processed':len(results),'phase_sets':sum(len(r['overlays']) for r in results),'images_generated':sum(len(s['slices']) for r in results for s in r['overlays'])},'candidates':results}
    dump_json(root/'detailed_review_v4.json',result)
    with (root/'detailed_review_v4.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f); w.writerow(['candidate_id','organ','triage_level','triage_score','phases','images','errors'])
        for r in results: w.writerow([r['candidate_id'],r['organ'],r['triage_level'],r['triage_score'],','.join(x['phase'] for x in r['overlays']),sum(len(x['slices']) for x in r['overlays']),len(r['errors'])])
    print(f'Completed detailed review: {len(results)} candidates, {result["summary"]["images_generated"]} images')
    print(f'JSON: {root/"detailed_review_v4.json"}')
    print(f'Images: {root}')

if __name__=='__main__': main()
