"""Bounded, offline Extruct adapter. No remote contexts, ID joins or inferred employers.

Evidence is an explicitly labelled canonical projection of direct schema properties,
not a claim that JSON serialization was visible prose on the source page.
"""
import json
from bs4 import BeautifulSoup
import extruct
from django.conf import settings
from .extraction import normalize

VERSION = "structured-v1"
RECIPE = {"engine": VERSION}
MAX_HTML = 6 * 1024 * 1024
MAX_BLOCK = 65536
MAX_TOTAL = 262144
MAX_ITEMS = 100
SCHEMA = {"https://schema.org", "http://schema.org"}


def scalar(value):
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    return normalize(value) if isinstance(value, str) and len(value) <= 300 else ""


def schema_type(value, kind):
    values = value if isinstance(value, list) else [value]
    return len(values) == 1 and values[0] in {kind, f"https://schema.org/{kind}", f"http://schema.org/{kind}"}


def projection(node, syntax, locator):
    type_key = "type" if syntax == "microdata" else "@type"
    props = node.get("properties", {}) if syntax == "microdata" else node
    if not isinstance(props, dict):
        return None
    kind = next((k for k in ("Person", "Organization") if schema_type(node.get(type_key), k)), None)
    name = scalar(props.get("name"))
    if not kind or not name:
        return None
    fields = {"name": name, "title": "", "email": "", "phone": "", "company": ""}
    if kind == "Person":
        if not 2 <= len(name.split()) <= 8 or len(name) > 100 or "@" in name:
            return None
        employer = props.get("worksFor")
        if isinstance(employer, list):
            employer = employer[0] if len(employer) == 1 else None
        if isinstance(employer, dict) and schema_type(employer.get(type_key), "Organization"):
            org_props = employer.get("properties", {}) if syntax == "microdata" else employer
            if isinstance(org_props, dict):
                fields["company"] = scalar(org_props.get("name"))
        fields.update(title=scalar(props.get("jobTitle")), email=scalar(props.get("email")),
                      phone=scalar(props.get("telephone")))
        for key, prefix in (("email", "mailto:"), ("phone", "tel:")):
            if fields[key].lower().startswith(prefix):
                fields[key] = fields[key][len(prefix):]
    else:
        fields["company"] = name
    # Only direct person properties enter evidence; descriptions, nested authors,
    # related people and employer contact points cannot become this person's inbox.
    evidence = (f"[Parsed {syntax} direct properties] " + normalize(json.dumps(
        {"schema_type": kind, **{k: v for k, v in fields.items() if v}}, ensure_ascii=False, sort_keys=True)))
    if len(evidence) > 1200:
        return None
    return {"kind": kind, "fields": fields, "evidence": evidence,
            "locator": f"{syntax}:{locator}:direct-properties", "syntax": syntax}


def entities(html):
    stats = {"parse_errors": 0, "limit_reached": False}
    if not settings.EXTRACTION_PACKS_ENABLED:
        return [], stats
    if len(html) > MAX_HTML:
        stats["limit_reached"] = True
        return [], stats
    soup = BeautifulSoup(html, "html.parser")
    found, used, visited = [], 0, 0

    def add(node, syntax, locator):
        nonlocal visited
        visited += 1
        if visited > MAX_ITEMS:
            stats["limit_reached"] = True
            return
        if isinstance(node, dict):
            item = projection(node, syntax, locator)
            if item:
                found.append(item)

    scripts = soup.select('script[type="application/ld+json"]', limit=33)
    if len(scripts) > 32:
        stats["limit_reached"] = True
    for index, script in enumerate(scripts[:32]):
        raw = script.get_text()
        used += len(raw)
        if len(raw) > MAX_BLOCK or used > MAX_TOTAL:
            stats["limit_reached"] = True
            continue
        try:
            # Selecting only json-ld disables RDFa and other processors entirely.
            nodes = extruct.extract(str(script), syntaxes=["json-ld"], errors="strict")["json-ld"]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                context = node.get("@context", "")
                if not isinstance(context, str) or context.rstrip("/") not in SCHEMA:
                    continue
                add(node, "json-ld", f"script[{index}]")
                graph = node.get("@graph", [])
                if isinstance(graph, list):
                    if len(graph) > MAX_ITEMS:
                        stats["limit_reached"] = True
                    for offset, child in enumerate(graph[:MAX_ITEMS]):
                        if isinstance(child, dict) and "@context" not in child:
                            add(child, "json-ld", f"script[{index}].graph[{offset}]")
        except (ValueError, TypeError, RecursionError):
            stats["parse_errors"] += 1

    roots = soup.select("[itemscope]", limit=501)
    if len(roots) > 500:
        stats["limit_reached"] = True
    for index, root in enumerate(roots[:500]):
        if root.find_parent(attrs={"itemscope": True}) or not any(
                root.get("itemtype") in {f"https://schema.org/{kind}", f"http://schema.org/{kind}"}
                for kind in ("Person", "Organization")):
            continue
        # itemref can cross card boundaries; no cross-container joining in v1.
        if root.has_attr("itemref") or root.select_one("[itemref]"):
            continue
        raw = str(root)
        used += len(raw)
        if len(raw) > MAX_BLOCK or used > MAX_TOTAL:
            stats["limit_reached"] = True
            continue
        try:
            nodes = extruct.extract(raw, syntaxes=["microdata"], errors="strict")["microdata"]
            for node in nodes:
                add(node, "microdata", f"itemscope[{index}]")
        except (ValueError, TypeError, RecursionError):
            stats["parse_errors"] += 1
    return found, stats
