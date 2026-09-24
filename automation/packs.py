"""First-party DOM recipe suggestions, not imported technology fingerprint data."""
from django.conf import settings
from leads.services.extraction import DEFAULT_SELECTORS, soup_for
from leads.services.structured import RECIPE, entities

VERSION = "packs-v1"
PACKS = {
    "wordpress": ('.wp-block-group', '.wp-block-heading', 'p'),
    "webflow": ('.w-dyn-item', 'h3', '.role, .job-title, h4, p'),
    "squarespace": ('.list-item', '.list-item-content__title', '.list-item-content__description'),
}


def hints(html):
    soup = soup_for(html)
    signals = {
        "wordpress": bool(soup.select_one('link[href*="/wp-content/"], img[src*="/wp-content/"], '
                                          'meta[name="generator"][content*="WordPress"], .wp-block-group')),
        "webflow": bool(soup.select_one('[data-wf-page], [data-wf-site], .w-dyn-list')),
        "squarespace": bool(soup.select_one('link[href*="squarespace"], [data-section-type="user-items-list"], '
                                            'meta[name="generator"][content*="Squarespace"]')),
    }
    return [name for name, seen in signals.items() if seen]


def suggestions(html_pages):
    if not settings.EXTRACTION_PACKS_ENABLED:
        return []
    result, seen = [], set()
    for html in html_pages:
        found, _ = entities(html)
        if any(item["kind"] == "Person" for item in found) and "structured" not in seen:
            result.append(dict(RECIPE))
            seen.add("structured")
        soup = soup_for(html)
        for platform in hints(html):
            row, name, title = PACKS[platform]
            if platform not in seen and soup.select_one(row):
                result.append({"row": row, "name": name, "title": title,
                               "email": DEFAULT_SELECTORS["email"], "phone": DEFAULT_SELECTORS["phone"],
                               "company": '.company', "evidence": ""})
                seen.add(platform)
    return result
