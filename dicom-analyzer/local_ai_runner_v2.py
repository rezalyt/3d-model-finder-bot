#!/usr/bin/env python3
import argparse, json, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path
import pydicom, dicom2nifti

ROIS=["liver","pancreas","gallbladder","spleen","kidney_left","kidney_right","adrenal_gland_left","adrenal_gland_right","aorta"]
SKIP=("topogram","scout","localizer","monitor","premonitor")

def inventory(zp):
    series={}
    with zipfile.ZipFile(zp) as z:
        for info in z.infolist():
            if info.is_dir(): continue
            p=Path(info.filename)
            if p.is_absolute() or ".." in p.parts: continue
            try:
                with z.open(info) as fh: ds=pydicom.dcmread(fh,stop_before_pixels=True,force=True)
                if not hasattr(ds,"Rows") or not hasattr(ds,"Columns"): continue
                uid=str(getattr(ds,"SeriesInstanceUID","unknown"))
                series.setdefault(uid,[]).append({"name":info.filename,"instance":int(getattr(ds,"InstanceNumber",0) or 0),"description":str(getattr(ds,"SeriesDescription","")),"modality":str(getattr(ds,"Modality",""))})
            except Exception: continue
    for items in series.values(): items.sort(key=lambda x:(x["instance"],x["name"]))
    return series

def copy_series(zp,items,dst):
    dst.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(zp) as z:
        for n,item in enumerate(items,1):
            with z.open(item["name"]) as src, open(dst/f"slice_{n:05d}.dcm","wb") as out: shutil.copyfileobj(src,out,4*1024*1024)

def gpu_available():
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception: return False

def run_one(zp,items,out_dir,gpu):
    out_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dicom_series_") as tmp:
        dcm=Path(tmp)/"dicom"; nii=Path(tmp)/"nifti"
        copy_series(zp,items,dcm); nii.mkdir()
        dicom2nifti.convert_directory(str(dcm),str(nii),compression=True,reorient=True)
        niftis=sorted(nii.glob("*.nii.gz"))
        if not niftis: raise RuntimeError("DICOM-to-NIfTI conversion produced no .nii.gz volume")
        cmd=["TotalSegmentator","-i",str(niftis[0]),"-o",str(out_dir/"segmentation"),"--task","total_v3","--roi_subset",*ROIS,"--statistics","--quiet"]
        if gpu: cmd += ["--device","gpu","--model_size","big"]
        else: cmd += ["--device","cpu","--model_size","small","--fast","--body_seg","--force_split","--nr_thr_resamp","1","--nr_thr_saving","1"]
        return subprocess.run(cmd,text=True,capture_output=True)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("zip",type=Path); ap.add_argument("--out",type=Path,default=Path("dicom-ai-result")); ap.add_argument("--test-one",action="store_true"); args=ap.parse_args()
    if not args.zip.exists(): raise SystemExit(f"ZIP not found: {args.zip}")
    series=inventory(args.zip); eligible=[]
    for uid,items in series.items():
        if len(items)<25 or items[0]["modality"]!="CT": continue
        desc=items[0]["description"].lower()
        if any(w in desc for w in SKIP): continue
        eligible.append((len(items),uid,items))
    eligible.sort(reverse=True)
    if not eligible: raise SystemExit("No suitable diagnostic CT series found")
    if args.test_one: eligible=eligible[:1]
    gpu=gpu_available(); args.out.mkdir(parents=True,exist_ok=True)
    report={"device":"gpu" if gpu else "cpu","model":"TotalSegmentator 2.18.0 / total_v3","model_size":"big" if gpu else "small","rois":ROIS,"series":[]}
    for idx,(count,uid,items) in enumerate(eligible,1):
        desc=items[0]["description"] or f"series_{idx:03d}"; safe="".join(c if c.isalnum() or c in "-_" else "_" for c in desc)[:80]; out=args.out/f"series_{idx:03d}_{safe}"
        try:
            proc=run_one(args.zip,items,out,gpu); ok=proc.returncode==0
            entry={"series_index":idx,"series_uid":uid,"description":desc,"instances":count,"status":"completed" if ok else "failed","returncode":proc.returncode,"result_dir":str(out),"stdout_tail":(proc.stdout or "")[-6000:],"stderr_tail":(proc.stderr or "")[-6000:]}
        except Exception as e: entry={"series_index":idx,"series_uid":uid,"description":desc,"instances":count,"status":"failed","returncode":-1,"error":str(e),"result_dir":str(out)}
        report["series"].append(entry); print(json.dumps(entry,ensure_ascii=False))
    (args.out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Report: {args.out/'report.json'}")

if __name__=="__main__": main()
