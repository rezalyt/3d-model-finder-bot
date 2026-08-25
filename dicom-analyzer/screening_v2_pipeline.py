#!/usr/bin/env python3
"""Second-pass triage for CT screening candidates.

Consumes screening.json produced by screening_pipeline.py. It does not claim
medical diagnosis. It applies conservative rule-based context filters to
separate likely physiologic/contrast-related findings from candidates that
still warrant review, while preserving the original observations.
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def phase_obs(candidate, phase):
    return [o for o in candidate.get("observations", []) if o.get("phase") == phase]


def triage(candidate):
    organ = str(candidate.get("organ", ""))
    obs = candidate.get("observations", [])
    phases = {str(o.get("phase")) for o in obs}
    reasons = []
    score = float(candidate.get("screening_priority", 0))

    if organ.startswith("kidney"):
        delayed = phase_obs(candidate, "delayed")
        portal = phase_obs(candidate, "portal")
        arterial = phase_obs(candidate, "arterial")
        delayed_vals = [float(o["mean_hu"]) for o in delayed if o.get("mean_hu") is not None]
        portal_vals = [float(o["mean_hu"]) for o in portal if o.get("mean_hu") is not None]
        arterial_vals = [float(o["mean_hu"]) for o in arterial if o.get("mean_hu") is not None]
        if delayed_vals and max(delayed_vals) >= 180 and portal_vals and max(portal_vals) <= 100:
            reasons.append("High delayed-phase renal attenuation with lower portal attenuation; likely contrast excretion/collecting-system effect.")
            score -= 55
        elif delayed_vals and max(delayed_vals) >= 150 and not portal_vals and not arterial_vals:
            reasons.append("High delayed-phase renal attenuation is nonspecific without a non-delayed comparison.")
            score -= 25

    if organ == "liver":
        portal_vals = [float(o["mean_hu"]) for o in phase_obs(candidate, "portal") if o.get("mean_hu") is not None]
        arterial_vals = [float(o["mean_hu"]) for o in phase_obs(candidate, "arterial") if o.get("mean_hu") is not None]
        native_vals = [float(o["mean_hu"]) for o in phase_obs(candidate, "native") if o.get("mean_hu") is not None]
        if portal_vals and max(portal_vals) >= 130 and max(portal_vals) <= 180:
            reasons.append("Portal-phase attenuation is within a common strongly enhancing range; vascular/background enhancement is a major alternative explanation.")
            score -= 35
        if arterial_vals and max(arterial_vals) >= 90 and max(arterial_vals) <= 130:
            reasons.append("Arterial-phase attenuation is compatible with enhancing background/vascular structures; not specific for a lesion.")
            score -= 25
        if native_vals and max(native_vals) < 30 and not portal_vals and not arterial_vals:
            reasons.append("Low attenuation on native phase without corroborating enhancement is nonspecific.")
            score -= 10

    if len(phases) >= 3:
        score += 12
        reasons.append("Observed across at least three phases; reproducibility supports manual review.")
    elif len(phases) == 2:
        score += 4
    else:
        score -= 5
        reasons.append("Observed in only one phase; confidence is limited.")

    score = max(0.0, min(100.0, score))
    if score >= 60:
        level = "HIGH_REVIEW"
    elif score >= 25:
        level = "REVIEW"
    else:
        level = "LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY"

    if not reasons:
        reasons.append("No strong rule-based context filter applied; manual image review remains necessary.")
    return level, round(score, 1), reasons


def main():
    ap = argparse.ArgumentParser(description="Second-pass CT screening candidate triage")
    ap.add_argument("--input", type=Path, default=Path("dicom-ai-result/screening.json"))
    ap.add_argument("--out", type=Path, default=Path("dicom-ai-result"))
    args = ap.parse_args()
    if not args.input.exists():
        raise SystemExit(f"Screening JSON not found: {args.input}")

    report = json.loads(args.input.read_text(encoding="utf-8"))
    candidates = report.get("candidates", [])
    triaged = []
    for candidate in candidates:
        level, score, reasons = triage(candidate)
        item = dict(candidate)
        item["triage_level"] = level
        item["triage_score"] = score
        item["triage_reasons"] = reasons
        triaged.append(item)

    triaged.sort(key=lambda x: x["triage_score"], reverse=True)
    counts = defaultdict(int)
    for c in triaged:
        counts[c["triage_level"]] += 1

    result = {
        "type": "research_screening_v2",
        "warning": "Rule-based triage only; not a medical diagnosis or exclusion of disease.",
        "source": str(args.input),
        "summary": {
            "input_candidates": len(candidates),
            "high_review": counts["HIGH_REVIEW"],
            "review": counts["REVIEW"],
            "likely_physiologic_or_low_priority": counts["LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY"],
        },
        "candidates": triaged,
    }

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "screening_v2.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with (args.out / "screening_v2.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["candidate_id", "organ", "phases", "original_priority", "triage_score", "triage_level", "reasons"])
        for c in triaged:
            w.writerow([
                c.get("candidate_id"), c.get("organ"), ",".join(c.get("phases", [])),
                c.get("screening_priority"), c.get("triage_score"), c.get("triage_level"),
                " | ".join(c.get("triage_reasons", [])),
            ])

    rows = []
    for c in triaged:
        obs = ", ".join(f"{o.get('phase')}: {float(o.get('mean_hu', 0)):.0f} HU" for o in c.get("observations", []))
        reasons = " ".join(c.get("triage_reasons", []))
        rows.append(
            f"<tr><td>{c.get('candidate_id')}</td><td>{c.get('organ')}</td><td>{c.get('triage_level')}</td>"
            f"<td>{c.get('triage_score'):.1f}</td><td>{obs}</td><td>{reasons}</td></tr>"
        )

    html = f"""<!doctype html><html><head><meta charset='utf-8'><title>CT screening v2</title>
<style>body{{font-family:Arial,sans-serif;margin:30px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:7px;text-align:left;vertical-align:top}}th{{background:#f0f0f0}}</style></head>
<body><h1>CT screening candidate triage v2</h1><p><b>Research/screening only.</b> Rule-based triage does not diagnose or exclude disease.</p>
<p>Input candidates: {len(candidates)}. HIGH_REVIEW: {counts['HIGH_REVIEW']}. REVIEW: {counts['REVIEW']}. LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY: {counts['LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY']}.</p>
<table><tr><th>ID</th><th>Organ</th><th>Level</th><th>Score</th><th>Observations</th><th>Reasoning</th></tr>{''.join(rows)}</table></body></html>"""
    (args.out / "screening_v2.html").write_text(html, encoding="utf-8")
    print(f"Input candidates: {len(candidates)}")
    print(f"HIGH_REVIEW: {counts['HIGH_REVIEW']}")
    print(f"REVIEW: {counts['REVIEW']}")
    print(f"LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY: {counts['LIKELY_PHYSIOLOGIC_OR_LOW_PRIORITY']}")
    print(f"JSON: {args.out / 'screening_v2.json'}")
    print(f"HTML: {args.out / 'screening_v2.html'}")


if __name__ == "__main__":
    main()
