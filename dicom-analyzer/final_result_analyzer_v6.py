#!/usr/bin/env python3
"""Unified post-processing for final abdominal screening + visual navigation.

Consumes final_screening.json and final_visual_review_v5.json. Does not rerun
DICOM conversion, segmentation, or candidate detection. Produces a compact
review queue grouped by organ/priority with links to visual folders and DICOM
navigation metadata when present.

Research/screening only; not a medical diagnosis.
"""
import argparse
import csv
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

LEVEL_ORDER = {"HIGH_REVIEW": 0, "REVIEW": 1, "LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY": 2}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def candidate_visual_map(report):
    out = {}
    for c in report.get("candidates", []):
        cid = str(c.get("candidate_id"))
        out[cid] = c
    return out


def enrich(candidate, visual):
    item = dict(candidate)
    item["visual"] = None
    if visual:
        item["visual"] = {
            "overlay_count": len(visual.get("overlays", [])),
            "overlays": visual.get("overlays", []),
            "errors": visual.get("errors", []),
        }
    phases = candidate.get("phases", [])
    observations = candidate.get("observations", [])
    hu = [float(o["mean_hu"]) for o in observations if o.get("mean_hu") is not None]
    item["phase_count"] = len(set(phases))
    item["hu_range"] = [min(hu), max(hu)] if hu else None
    item["has_multiphase_support"] = item["phase_count"] >= 2
    item["has_visuals"] = bool(visual and visual.get("overlays"))
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screening", type=Path, default=Path("dicom-ai-result/final_screening/final_screening.json"))
    ap.add_argument("--visual", type=Path, default=Path("dicom-ai-result/final_visual_review_v5/final_visual_review_v5.json"))
    ap.add_argument("--out", type=Path, default=Path("dicom-ai-result/final_result_v6"))
    args = ap.parse_args()
    if not args.screening.exists():
        raise SystemExit(f"Screening JSON not found: {args.screening}")
    if not args.visual.exists():
        raise SystemExit(f"Visual review JSON not found: {args.visual}")

    screening = load_json(args.screening)
    visual = load_json(args.visual)
    vmap = candidate_visual_map(visual)
    candidates = [enrich(c, vmap.get(str(c.get("candidate_id")))) for c in screening.get("candidates", [])]
    candidates.sort(key=lambda c: (LEVEL_ORDER.get(c.get("triage_level"), 9), -float(c.get("triage_score", 0))))

    by_organ = defaultdict(list)
    for c in candidates:
        by_organ[c.get("organ", "unknown")].append(c)
    levels = Counter(c.get("triage_level", "UNKNOWN") for c in candidates)

    review_queue = [c for c in candidates if c.get("triage_level") in {"HIGH_REVIEW", "REVIEW"}]
    summary = {
        "input_candidates": len(candidates),
        "high_review": levels.get("HIGH_REVIEW", 0),
        "review": levels.get("REVIEW", 0),
        "low_priority": levels.get("LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY", 0),
        "with_visuals": sum(1 for c in candidates if c.get("has_visuals")),
        "with_multiphase_support": sum(1 for c in candidates if c.get("has_multiphase_support")),
        "organs": {k: len(v) for k, v in sorted(by_organ.items())},
    }

    result = {
        "type": "research_screening_final_result_v6",
        "warning": "Automated candidate prioritization only; not a medical diagnosis or exclusion of disease.",
        "sources": {"screening": str(args.screening), "visual_review": str(args.visual)},
        "summary": summary,
        "review_queue": review_queue,
        "candidates": candidates,
    }

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "final_result_v6.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    with (args.out / "final_result_v6.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "organ", "region_type", "level", "score", "phases", "phase_count", "volume_mm3", "hu_min", "hu_max", "visuals", "reasons"])
        for c in candidates:
            obs = c.get("observations", [])
            hu = c.get("hu_range") or ["", ""]
            w.writerow([
                c.get("candidate_id"), c.get("organ"), obs[0].get("region_type", "") if obs else "",
                c.get("triage_level"), c.get("triage_score"), ",".join(c.get("phases", [])),
                c.get("phase_count"), obs[0].get("volume_mm3", "") if obs else "",
                hu[0], hu[1], c.get("has_visuals"), " | ".join(c.get("triage_reasons", [])),
            ])

    rows = []
    for c in candidates:
        cid = html.escape(str(c.get("candidate_id")))
        organ = html.escape(str(c.get("organ")))
        level = html.escape(str(c.get("triage_level")))
        score = float(c.get("triage_score", 0))
        phases = html.escape(", ".join(c.get("phases", [])))
        obs = c.get("observations", [])
        hu = c.get("hu_range")
        hu_text = f"{hu[0]:.1f}…{hu[1]:.1f} HU" if hu else "n/a"
        visuals = c.get("visual") or {}
        overlay_count = visuals.get("overlay_count", 0)
        reason = html.escape(" ".join(c.get("triage_reasons", [])))
        rows.append(f"<tr><td>{cid}</td><td>{organ}</td><td>{level}</td><td>{score:.1f}</td><td>{phases}</td><td>{hu_text}</td><td>{overlay_count}</td><td>{reason}</td></tr>")

    organ_rows = "".join(f"<tr><td>{html.escape(str(k))}</td><td>{v}</td></tr>" for k, v in summary["organs"].items())
    text = f"""<!doctype html><html><head><meta charset='utf-8'><title>Final abdominal screening result v6</title>
<style>body{{font-family:Arial,sans-serif;margin:30px}}table{{border-collapse:collapse;width:100%;margin-bottom:24px}}td,th{{border:1px solid #ccc;padding:7px;text-align:left;vertical-align:top}}th{{background:#f0f0f0}}.note{{padding:10px;background:#f7f7f7;margin-bottom:18px}}</style></head>
<body><h1>Final abdominal screening result v6</h1>
<div class='note'><b>Research/screening only.</b> Automated prioritization is not a diagnosis and does not exclude disease.</div>
<p>Candidates: {summary['input_candidates']} &nbsp; HIGH_REVIEW: {summary['high_review']} &nbsp; REVIEW: {summary['review']} &nbsp; Low priority: {summary['low_priority']} &nbsp; Visualized: {summary['with_visuals']}</p>
<h2>Distribution by organ</h2><table><tr><th>Organ</th><th>Candidates</th></tr>{organ_rows}</table>
<h2>Review queue</h2><table><tr><th>ID</th><th>Organ</th><th>Level</th><th>Score</th><th>Phases</th><th>HU range</th><th>Images</th><th>Reason</th></tr>{''.join(rows)}</table>
</body></html>"""
    (args.out / "final_result_v6.html").write_text(text, encoding="utf-8")

    print(f"Input candidates: {summary['input_candidates']}")
    print(f"HIGH_REVIEW: {summary['high_review']}")
    print(f"REVIEW: {summary['review']}")
    print(f"LOW_PRIORITY: {summary['low_priority']}")
    print(f"With visuals: {summary['with_visuals']}")
    print(f"With multiphase support: {summary['with_multiphase_support']}")
    print(f"JSON: {args.out / 'final_result_v6.json'}")
    print(f"HTML: {args.out / 'final_result_v6.html'}")
    print(f"CSV: {args.out / 'final_result_v6.csv'}")

if __name__ == "__main__":
    main()
