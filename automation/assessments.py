"""Why a probed site did or didn't yield contacts, recorded for later analysis.

Everything here is aggregate: counts, reason codes, page kinds and framework hints. Names,
emails and phone numbers are never written to an assessment, matching the recon invariant.
"""
from urllib.parse import urlsplit

from discovery.ranking import company_page_tier, direct_contact_page_intent
from leads.services.network import origin

# pause_job() messages that describe our own configuration changes or operator actions,
# not anything learned about the site. These never produce an assessment.
NOT_ABOUT_THE_SITE = (
    "Source scope, campaign policy or authorization changed",
    "Paused by operator", "Operator rollback", "Source settings changed",
    "Campaign automation policy changed", "Source setup queued",
)

FAILURE_OUTCOMES = (
    ("canonical origin conflict", "canonical_conflict"),
    ("access challenge", "blocked"), ("403", "blocked"), ("401", "blocked"), ("blocked", "blocked"),
    ("robots", "robots_disallowed"),
    ("collection restriction", "collection_prohibited"),
    ("outside the approved scope", "redirect_out_of_scope"),
    ("no available html pages", "no_pages"),
    ("too many redirects", "unavailable"), ("page unavailable", "unavailable"), ("timed out", "unavailable"),
)


def page_kind(url, source_url):
    path = urlsplit(url).path or "/"
    if path == "/" or url.rstrip("/") == source_url.rstrip("/"):
        return "start"
    if direct_contact_page_intent(url):
        return "team"
    tier = company_page_tier(url)
    return {1: "about", 2: "contact"}.get(tier, "other")


def build(source, page_stats, page_metadata):
    """Derive outcome, reasons, future options, facts and summary from per-page evaluation stats.

    ``page_stats`` maps url -> evaluate() stats; ``page_metadata`` maps url -> probe recon metadata.
    """
    pages, totals = [], {"people_named": 0, "people_titled": 0, "sales_titled": 0,
                         "people_with_any_contact": 0, "contacts_accepted": 0,
                         "site_email_links": 0, "site_phone_links": 0}
    rejections, frameworks, packs, js_heavy, protected = {}, set(), set(), False, 0
    for url in dict.fromkeys(list(page_metadata) + list(page_stats)):
        stats, meta = page_stats.get(url, {}), page_metadata.get(url, {})
        signals = stats.get("signals", {})
        mechanisms = meta.get("contact_mechanisms", {})
        pages.append({"url": url, "kind": page_kind(url, source.url), "result": stats.get("result", "not_evaluated"),
                      "observed_with": stats.get("row", ""),
                      "matched_cards": stats.get("matched_cards", 0), "named": signals.get("name", 0),
                      "titled": signals.get("titled"), "sales_titled": signals.get("role", 0),
                      "with_contact": signals.get("contact", 0), "accepted": stats.get("accepted_records", 0),
                      "email_links": mechanisms.get("mailto", 0), "phone_links": mechanisms.get("tel", 0)})
        totals["people_named"] += signals.get("name", 0)
        totals["people_titled"] += signals.get("titled") or 0
        totals["sales_titled"] += signals.get("role", 0)
        totals["people_with_any_contact"] += signals.get("contact", 0)
        totals["contacts_accepted"] += stats.get("accepted_records", 0)
        totals["site_email_links"] += mechanisms.get("mailto", 0)
        totals["site_phone_links"] += mechanisms.get("tel", 0)
        for key, value in stats.get("primary_rejections", {}).items():
            rejections[key] = rejections.get(key, 0) + value
        frameworks.update(h for h in meta.get("framework_hints", []) if h != "unknown HTML")
        packs.update(meta.get("recipe_pack_hints", []))
        js_heavy = js_heavy or bool(meta.get("javascript_heavy_hint"))
        protected += mechanisms.get("protected_emails", 0)
    # Titles are unknown (not zero) for stats recorded before titled was tracked.
    titles_known = all(page["titled"] is not None for page in pages if page["matched_cards"])

    named, accepted = totals["people_named"], totals["contacts_accepted"]
    site_contacts = totals["site_email_links"] + totals["site_phone_links"]
    kinds = {page["kind"] for page in pages}
    reasons, options = [], []
    if accepted:
        outcome = "contacts_found"
    elif named and not totals["people_with_any_contact"]:
        outcome = "people_without_personal_contacts"
    elif named:
        outcome = "contacts_rejected"
    elif any(page["matched_cards"] for page in pages):
        outcome = "cards_without_people"
    else:
        outcome = "no_people_listed"

    if not accepted:
        if not kinds & {"team", "about", "contact"}:
            reasons.append("no_team_about_or_contact_page_linked")
        if named and titles_known and totals["people_titled"] and not totals["sales_titled"]:
            reasons.append("non_sales_titles_only")
            options.append("non_sales_people_listed")
        if named and not totals["people_with_any_contact"]:
            if site_contacts:
                reasons.append("only_shared_company_contact")
                options.append("shared_contact_leads")
            else:
                reasons.append("no_contact_details_published")
        if rejections.get("shared_or_global_contact"):
            reasons.append("contacts_shared_across_people")
        if rejections.get("missing_role"):
            reasons.append("missing_sales_role")
        if rejections.get("missing_employer"):
            # Structured Person data without worksFor. The employer stays unknown; a future option is
            # letting an operator confirm the source site's own company as the employer.
            reasons.append("structured_people_without_employer")
            options.append("operator_confirmed_employer")
        if rejections.get("placeholder_name") or rejections.get("placeholder_contact"):
            reasons.append("placeholder_or_decoy_contacts")
        if protected:
            # The site hides emails from crawlers on purpose; respected, not decoded.
            reasons.append("emails_hidden_by_protection")
        if rejections.get("ambiguous_container"):
            reasons.append("ambiguous_card_layout")
        if not named and site_contacts:
            reasons.append("contact_details_without_people")
        if js_heavy:
            reasons.append("javascript_heavy_page")
            options.append("browser_collector")
        if not named and frameworks:
            options.append("platform_recipe_pack")

    titled = str(totals["people_titled"]) if titles_known else "unknown"
    summary = (f"{named} people named ({titled} with titles, {totals['sales_titled']} sales titles), "
               f"{totals['people_with_any_contact']} with a contact on their card, {accepted} accepted; "
               f"site shows {totals['site_email_links']} email and {totals['site_phone_links']} phone link(s) "
               f"across {len(pages)} page(s): {', '.join(sorted(kinds)) or 'none'}.")
    facts = {"pages": pages, "totals": totals, "titles_known": titles_known, "rejections": rejections,
             "protected_emails": protected,
             "frameworks": sorted(frameworks), "recipe_pack_hints": sorted(packs), "javascript_heavy": js_heavy}
    return outcome, reasons, options, facts, summary[:500]


def _specific_row(stats):
    """A class/attribute card selector (e.g. .team-card) or structured data, rather than a generic
    catch-all like article:has(h3) that also sweeps up blog teasers and other non-people blocks."""
    row = stats.get("row", "")
    return bool(stats.get("matched_cards")) and (row.startswith((".", "[")) or row == "structured-v1")


def _observation_rank(stats):
    signals = stats.get("signals", {})
    return (stats.get("accepted_records", 0), signals.get("contact", 0), _specific_row(stats),
            signals.get("titled") or 0, signals.get("name", 0), stats.get("matched_cards", 0))


def richest(stats_by_recipe):
    """Per page, keep the evaluation that observed the most — this describes what the site
    publishes, so it shouldn't depend on which of several tied recipe variants won."""
    best = {}
    for page_stats in stats_by_recipe:
        for url, stats in page_stats.items():
            if url not in best or _observation_rank(stats) > _observation_rank(best[url]):
                best[url] = stats
    return best


def record(job, page_stats, page_metadata):
    """``page_stats`` is one {url: stats} mapping or a list of them (one per recipe tried)."""
    from .models import SiteAssessment
    if isinstance(page_stats, list):
        page_stats = richest(page_stats)
    outcome, reasons, options, facts, summary = build(job.source, page_stats, page_metadata)
    assessment, _ = SiteAssessment.objects.update_or_create(job=job, generation=job.generation, defaults={
        "source": job.source, "origin": origin(job.source.url), "outcome": outcome, "reasons": reasons,
        "future_options": options, "facts": facts, "summary": summary})
    return assessment


def record_failure(job, message):
    """Record a site-level failure (blocked, robots, redirect, unavailable) from a pause message."""
    from .models import SiteAssessment
    if message.startswith("Recipe review needed") or any(message.startswith(p) for p in NOT_ABOUT_THE_SITE):
        return None
    lowered = message.lower()
    outcome = next((code for needle, code in FAILURE_OUTCOMES if needle in lowered), "setup_error")
    existing = SiteAssessment.objects.filter(job=job, generation=job.generation).first()
    if existing and outcome == "setup_error":
        # Unrecognized text on an already-assessed site (e.g. an operator's closing note) is kept
        # as a note; it must not overwrite what was actually observed on the site.
        existing.summary = (f"{existing.summary} Note: {message}")[:500]
        existing.save(update_fields=["summary", "updated_at"])
        return existing
    if existing:
        # A later-stage failure (e.g. canary) keeps the recipe-stage facts and adds the new reason.
        existing.outcome = outcome
        existing.reasons = list(dict.fromkeys(existing.reasons + [outcome]))
        existing.summary = (f"{existing.summary} Then: {message}")[:500]
        existing.save(update_fields=["outcome", "reasons", "summary", "updated_at"])
        return existing
    return SiteAssessment.objects.create(job=job, generation=job.generation, source=job.source,
        origin=origin(job.source.url), outcome=outcome, reasons=[outcome], facts={}, summary=message[:500])
