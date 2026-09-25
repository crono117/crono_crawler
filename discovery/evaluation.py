"""Offline discovery evaluation against operator labels. No network requests.

Label file (JSON)::

    {
      "version": 1,
      "pages": [
        {"url": "https://agent.example/our-team/", "label": "reps", "anchor": "Meet our team"},
        {"url": "https://agent.example/blog/", "label": "irrelevant"}
      ],
      "sites": ["https://expected-agent.example"]
    }

Page labels: ``reps`` (names sales reps / agents), ``team_no_reps`` (people page without
target reps) or ``irrelevant``. Blank labels are skipped, so an exported template can be
labeled gradually. ``sites`` are exact origins discovery is expected to find.
"""
import json

from .ranking import current_candidate_score, ordinary_page_eligible
from .yields import safe_origin

LABELS = ("reps", "team_no_reps", "irrelevant")
POSITIVE = "reps"
MAX_PAGES = 5000


def load_labels(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Expected a version 1 label file.")
    pages = []
    for row in data.get("pages", [])[:MAX_PAGES]:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            raise ValueError("Each page needs a url.")
        label = row.get("label", "")
        if label == "":
            continue
        if label not in LABELS:
            raise ValueError(f"Unknown label {label!r}; use one of {', '.join(LABELS)} or leave it blank.")
        pages.append({"url": row["url"], "label": label, "anchor": str(row.get("anchor", ""))[:200]})
    sites = [site for site in data.get("sites", []) if isinstance(site, str) and site]
    return pages, sites


def export_template(campaign, limit=150):
    """Label template from the campaign's most recent page candidates (labels left blank)."""
    rows = campaign.urls.filter(kind="page").order_by("-id")[:limit]
    return {"version": 1,
            "pages": [{"url": row.url, "label": "", "anchor": row.label, "method": row.method,
                       "decision": row.decision, "score": row.score} for row in rows],
            "sites": []}


def average_precision(ranked):
    hits, total = 0, 0.0
    for index, positive in enumerate(ranked, start=1):
        if positive:
            hits += 1
            total += hits / index
    return total / hits if hits else 0.0


def evaluate(campaign, pages, sites, ks=(10, 50)):
    """Score labeled pages with the live ranker and gate; measure site recall from saved URLs."""
    scored = []
    for page in pages:
        score, _ = current_candidate_score(campaign, page["url"], page["anchor"])
        eligible, _, _ = ordinary_page_eligible(campaign, page["url"], page["anchor"])
        scored.append((score, page["label"] == POSITIVE, eligible, page["url"]))
    scored.sort(key=lambda item: (-item[0], item[3]))
    ranked = [positive for _, positive, _, _ in scored]
    positives = sum(ranked)
    report = {"pages": len(scored), "positives": positives,
              "average_precision": round(average_precision(ranked), 3), "precision_at": {}}
    for k in ks:
        top = ranked[:k]
        report["precision_at"][k] = round(sum(top) / len(top), 3) if top else 0.0
    true_pos = sum(1 for _, positive, eligible, _ in scored if positive and eligible)
    passed = sum(1 for _, _, eligible, _ in scored if eligible)
    report["gate"] = {"passed": passed,
                      "precision": round(true_pos / passed, 3) if passed else 0.0,
                      "recall": round(true_pos / positives, 3) if positives else 0.0}
    report["missed_reps"] = [url for _, positive, eligible, url in scored if positive and not eligible][:20]
    report["false_passes"] = [url for _, positive, eligible, url in scored if eligible and not positive][:20]
    if sites:
        expected = {safe_origin(site) for site in sites} - {""}
        found = {}
        for item_origin, method in campaign.urls.filter(origin__in=expected).values_list("origin", "method"):
            found.setdefault(item_origin, set()).add(method)
        report["sites"] = {"expected": len(expected), "found": len(found),
                           "recall": round(len(found) / len(expected), 3) if expected else 0.0,
                           "by_method": {method: sum(1 for methods in found.values() if method in methods)
                                         for method in sorted(set().union(*found.values()))} if found else {},
                           "missing": sorted(expected - set(found))[:20]}
    return report
