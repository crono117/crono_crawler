"""Deterministic proposals and local evidence checks. No contact inference or model calls."""
from collections import Counter
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit
from leads.services.extraction import (DEFAULT_SELECTORS, EMAIL, GENERIC, SALES_ROLE, contact_evidence,
    evidence_node, normalize, selected_text, soup_for, tags, validate_recipe, validate_record)
from leads.services.network import in_scope
from discovery.ranking import clean_url, rank

PROBE_VERSION = "1.0"
FIELDS = ("name", "title", "email", "phone", "company")
PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ().-]{6,}\d)(?!\w)")
FIELD_OPTIONS = [
    {"name": DEFAULT_SELECTORS["name"], "title": DEFAULT_SELECTORS["title"]},
    {"name": "h3", "title": "h4, .position, .designation, .job, .job-title, .role, .title"},
    {"name": "h2", "title": "h3, h4, .position, .designation, .role, .title"},
    {"name": "h3, h2", "title": "p"},
]
ROW_CLASSES = re.compile(r"^(?:team|staff|person|agent|employee|member|profile|bio|sales)[-_](?:member|card|item|tile|entry|profile|bio|person|rep)(?:s)?$", re.I)


def proposals(html_pages):
    rows = set(DEFAULT_SELECTORS["row"].split(", "))
    for html in html_pages:
        soup = soup_for(html)
        for node in soup.find_all(["article", "section", "li", "div"], limit=5000):
            for token in node.get("class", []):
                # No IDs, inline data, names, email text or arbitrary class strings in recon.
                if ROW_CLASSES.fullmatch(token):
                    rows.add("." + token)
        if soup.select("article h3"):
            rows.add("article:has(h3)")
    return [{"row": row, **fields, "email": DEFAULT_SELECTORS["email"], "phone": DEFAULT_SELECTORS["phone"],
             "company": ".company", "evidence": ""} for row in sorted(rows) for fields in FIELD_OPTIONS][:64]


def evaluate(html, source, recipe):
    selectors = DEFAULT_SELECTORS | validate_recipe(recipe)
    soup = soup_for(html)
    rows = soup.select(selectors["row"], limit=501)
    counts, candidates, shape = Counter(), [], []
    global_text = " ".join(contact_evidence(node) for node in soup.select("header, footer, nav, [role=navigation], [role=contentinfo]"))
    global_emails = {value.casefold() for value in EMAIL.findall(global_text)}
    global_phones = {re.sub(r"\D", "", value) for value in PHONE.findall(global_text)}
    signals = Counter()
    for row in rows[:500]:
        fields = {key: selected_text(row, selectors[key]) for key in FIELDS}
        signals["name"] += int(len(fields["name"].split()) >= 2)
        signals["role"] += int(bool(fields["title"] and SALES_ROLE.search(fields["title"])))
        signals["contact"] += int(bool(fields["email"] or fields["phone"]))
        shape.append([node.name for node in row.find_all(True, limit=40)])
        names = {normalize(node.get_text(" ", strip=True)) for node in row.select(selectors["name"]) if normalize(node.get_text(" ", strip=True))}
        if row.find_parent(["header", "footer", "nav"]) or row.select_one("header, footer, nav, form") or len(names) != 1:
            counts["ambiguous_container"] += 1
            continue
        evidence = evidence_node(row, selectors["evidence"])
        if evidence is None:
            counts["missing_evidence_container"] += 1
            continue
        stats = {}
        record = validate_record(fields, contact_evidence(evidence), source, stats)
        if not record:
            counts.update(stats.get("rejected", {}))
        elif source.require_sales_role and not record["title"]:
            counts["missing_role"] += 1
        else:
            candidates.append(record)
    # A repeated switchboard or generic footer inbox never becomes a direct contact.
    email_names, phone_names = {}, {}
    for record in candidates:
        email_names.setdefault(record["email"], set()).add(record["name"].casefold())
        phone_names.setdefault(re.sub(r"\D", "", record["phone"]), set()).add(record["name"].casefold())
    accepted = {}
    for record in candidates:
        email, digits = record["email"], re.sub(r"\D", "", record["phone"])
        if email and (email in global_emails or email.split("@")[0] in GENERIC or len(email_names[email]) > 1):
            record["email"] = ""
        if digits and (digits in global_phones or len(phone_names[digits]) > 1):
            record["phone"] = ""
        if not record["email"] and not record["phone"]:
            counts["shared_or_global_contact"] += 1
            continue
        key = (record["name"].casefold(), record["email"], record["phone"])
        if key in accepted:
            counts["duplicate_card"] += 1
        else:
            accepted[key] = record
    total = min(len(rows), 500)
    result = "contacts_accepted" if accepted else ("no_matching_cards" if not total else
        "cards_without_contacts" if counts.get("missing_contact", 0) + counts.get("shared_or_global_contact", 0) == total else "contacts_or_evidence_invalid")
    stats = {"result": result, "matched_cards": total, "accepted_records": len(accepted),
             "rejected_records": sum(counts.values()), "primary_rejections": dict(counts), "signals": dict(signals),
             "row_limit_reached": len(rows) > 500,
             "structure_hash": hashlib.sha256(json.dumps(sorted({tuple(s) for s in shape})).encode()).hexdigest()}
    return list(accepted.values()), tags(normalize(soup.get_text(" ", strip=True))), stats


def readiness(page_stats):
    cards = sum(s["matched_cards"] for s in page_stats)
    accepted = sum(s["accepted_records"] for s in page_stats)
    roles = sum(s["signals"].get("role", 0) for s in page_stats)
    errors = Counter()
    for stats in page_stats:
        errors.update(stats["primary_rejections"])
    hard = sum(value for key, value in errors.items() if key not in ("missing_contact", "non_sales_role", "shared_or_global_contact"))
    score = (20 if cards >= 2 else 15 if cards else 0)
    score += 20 if cards and roles / cards >= .8 else 0
    score += 25 if accepted else 0
    score += 25 if accepted and not hard else 0
    if hard or any(s["row_limit_reached"] for s in page_stats):
        score -= 25
    return max(0, score)


def recon_page(response, source, campaign):
    html, soup = response.text, soup_for(response.text)
    hints = []
    for name, markers in {"wordpress": ("wp-content", "wp-includes"), "webflow": ("data-wf-page", "webflow.js"),
                          "squarespace": ("squarespace",), "hubspot": ("hs-scripts", "hubspot")}.items():
        if any(marker in html.lower() for marker in markers):
            hints.append(name)
    metadata = {"url": response.url, "status": response.status, "redirect_chain": response.redirect_chain,
        "content_type": response.headers.get("content-type", "")[:160],
        "encoding": response.headers.get("content-type", "").split("charset=")[-1][:40] if "charset=" in response.headers.get("content-type", "") else "utf-8 (default)",
        "content_hash": hashlib.sha256(response.body).hexdigest(), "response_bytes": len(response.body),
        "framework_hints": hints or ["unknown HTML"], "javascript_heavy_hint": len(soup.get_text(" ", strip=True)) < 200 and "<script" in html.lower(),
        "contact_mechanisms": {"mailto": len(soup.select("a[href^='mailto:']")), "tel": len(soup.select("a[href^='tel:']")),
            "visible_email_count": len(EMAIL.findall(soup.get_text(" ", strip=True))), "visible_phone_count": len(PHONE.findall(soup.get_text(" ", strip=True)))},
        "candidate_containers": []}
    seen = set()
    for recipe in proposals([html]):
        if recipe["row"] in seen:
            continue
        seen.add(recipe["row"])
        _, _, stats = evaluate(html, source, recipe)
        if stats["matched_cards"]:
            metadata["candidate_containers"].append({"selector": recipe["row"], "matches": stats["matched_cards"], "signals": stats["signals"]})
    links = {}
    for node in soup.select("a[href]")[:2000]:
        try:
            url = clean_url(urljoin(response.url, node["href"]))
        except (ValueError, UnicodeError):
            continue
        if in_scope(source, url) and not urlsplit(url).query:
            score, _ = rank(campaign, url, node.get_text(" ", strip=True)[:200])
            if score >= campaign.min_score:
                links[url] = score
    return metadata, sorted(links, key=links.get, reverse=True)[:50]
