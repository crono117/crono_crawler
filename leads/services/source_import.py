"""Metadata-only source import. Never authorizes, fetches, or queues collection."""
import json
import unicodedata
from typing import NoReturn
from urllib.parse import unquote, urlsplit

from django.core.exceptions import ValidationError
from django.db import transaction

from discovery.importing import metadata_url
from leads.models import CATEGORIES, Source
from leads.services.network import canonical_url, in_scope

MAX_BYTES = 262144
MAX_ROWS = 100
LIMITS = {"max_pages": (1, 500, 5), "max_depth": (0, 5, 1),
          "delay_seconds": (2, 3600, 5), "interval_hours": (1, 8760, 168)}
FIELDS = {"name", "url", "company", "category", "allowed_paths", "follow_links", "discover_external", *LIMITS}


def _error(index, field, message) -> NoReturn:
    # Field names here are constants, never untrusted dictionary keys or values.
    raise ValueError(f"Row {index}: {field} {message}")


def _controls(value):
    return any(unicodedata.category(c).startswith("C") for c in value)


def _plain_path(value):
    return (isinstance(value, str) and 1 <= len(value) <= 1500 and value.startswith("/")
            and not any(c in value for c in ("%", "?", "#", "\\", "..", "//"))
            and "." not in value.split("/") and not _controls(value)
            and not any(c.isspace() for c in value))


def validate_document(document):
    """Return fresh normalized rows; validate the complete batch before any writes."""
    if not isinstance(document, dict) or set(document) != {"schema_version", "sources"}:
        raise ValueError("Use an object with exactly schema_version and sources.")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("schema_version must be integer 1.")
    rows = document["sources"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError("sources must be a list of 1–100 objects.")
    validated = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - FIELDS:
            _error(index, "fields", "must be supported source settings only; unknown fields are not accepted.")
        clean = {}
        for field in ("name", "company"):
            value = row.get(field, "")
            if (not isinstance(value, str) or len(value) > 160 or _controls(value)
                    or any(c.isspace() and c != " " for c in value)):
                _error(index, field, "must be a string of at most 160 characters without controls or non-space whitespace.")
            clean[field] = value.strip()
            if field == "name" and not clean[field]:
                _error(index, field, "is required and must not be empty.")
        raw = row.get("url")
        try:
            if (not isinstance(raw, str) or not raw or len(raw) > 1500 or _controls(raw)
                    or any(c.isspace() for c in raw) or "\\" in raw):
                raise ValueError
            decoded = unquote(raw, errors="strict")
            if _controls(decoded) or "\\" in decoded:
                raise ValueError
            metadata_url(raw)  # Content/local-destination restrictions, without DNS.
            clean["url"] = canonical_url(raw)  # Preserve query order and tracking.
            path = unquote(urlsplit(clean["url"]).path, errors="strict") or "/"
            if not _plain_path(path):
                raise ValueError
        except (ValueError, UnicodeError, ValidationError):
            _error(index, "url", "must be a public HTTP(S) content URL with standard port, no credentials, whitespace, controls, backslashes or ambiguous path.")
        category = row.get("category", "merchant_services")
        if not isinstance(category, str) or category not in dict(CATEGORIES):
            _error(index, "category", "must be a supported business type.")
        clean["category"] = category
        paths = row.get("allowed_paths", [path])
        if not isinstance(paths, list) or not 1 <= len(paths) <= 10 or not all(_plain_path(p) for p in paths):
            _error(index, "allowed_paths", "must contain 1–10 plain decoded path prefixes (1–1500 characters), starting with / and without escapes, whitespace, query, fragment or traversal.")
        clean["allowed_paths"] = "\n".join(paths)
        if not in_scope(Source(url=clean["url"], allowed_paths=clean["allowed_paths"], allow_homepage=False), clean["url"]):
            _error(index, "allowed_paths", "must include the starting URL path.")
        for field, (low, high, default) in LIMITS.items():
            value = row.get(field, default)
            if type(value) is not int or not low <= value <= high:
                _error(index, field, f"must be an integer from {low} to {high}.")
            clean[field] = value
        for field in ("follow_links", "discover_external"):
            value = row.get(field, False)
            if type(value) is not bool:
                _error(index, field, "must be true or false.")
            clean[field] = value
        validated.append(clean)
    # Bound direct service callers too. Compact UTF-8 cannot exceed the raw upload.
    if len(json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_BYTES:
        raise ValueError("JSON source data exceeds 256 KiB.")
    return validated


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object fields are not accepted.")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON numbers are not accepted.")


def parse_upload(body):
    """Parse bounded UTF-8 (optional BOM), without lossy decoding or JSON extensions."""
    if not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise ValueError("Upload a UTF-8 JSON file no larger than 256 KiB.")
    try:
        text = body.decode("utf-8-sig")
        # Bound nesting before json.loads; brackets inside strings do not count.
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > 8:
                    raise ValueError
            elif char in "]}":
                depth -= 1
        document = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Use valid UTF-8 JSON with unique object fields, finite numbers and no deep nesting.") from None
    validate_document(document)
    return document


@transaction.atomic
def import_sources(document, *, apply=False):
    rows = validate_document(document)
    results, seen = [], {}
    for index, row in enumerate(rows, 1):
        url = row["url"]
        if url in seen:
            outcome, source_id = "duplicate_in_file", seen[url]
        else:
            source = Source.objects.filter(url=url).first()
            outcome = "existing" if source else "would_create"
            if apply and source is None:
                defaults = {key: value for key, value in row.items() if key != "url"}
                defaults.update(active=False, approved=False, approval_kind="operator", approval_notes="",
                                setup_mode="rules_only", collector="http", extractor="rules", require_sales_role=True,
                                recipe={}, allow_homepage=False)
                # Unique URL plus get_or_create handles a competing insert without updating it.
                source, created = Source.objects.get_or_create(url=url, defaults=defaults)
                outcome = "created" if created else "existing"
            source_id = source.pk if source else None
            seen[url] = source_id
        results.append({**row, "row": index, "source_id": source_id, "outcome": outcome})
    return {"results": results, "created": sum(r["outcome"] == "created" for r in results),
            "would_create": sum(r["outcome"] == "would_create" for r in results),
            "skipped": sum(r["outcome"] in ("existing", "duplicate_in_file") for r in results)}
