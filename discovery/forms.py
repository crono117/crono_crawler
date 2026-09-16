import re
from django import forms
from leads.models import Source
from leads.services.network import in_scope
from .models import Campaign
from .providers import search_ready
from .ranking import CONTACT_EXCLUSIONS, lines


class CampaignForm(forms.ModelForm):
    add_contact_exclusions = forms.BooleanField(required=False, label="Add contact discovery exclusions",
        help_text="Adds path filters for blogs, articles, products, software and card machines, plus Facebook, LinkedIn and X domains. Keeps your other exclusions. Review before using on software reseller directories.")
    class Meta:
        model = Campaign
        fields = ["name", "category", "sources", "keywords", "sales_terms", "exclusions", "add_contact_exclusions", "region", "use_sitemaps",
                  "max_pages", "max_depth", "max_candidates", "max_new_domains", "daily_requests", "min_score",
                  "interval_hours", "search_enabled", "search_queries", "daily_search_limit"]
        widgets = {name: forms.Textarea(attrs={"rows": 4}) for name in ("keywords", "sales_terms", "exclusions", "search_queries")}
        labels = {"category": "Campaign focus", "sources": "Approved starting sources", "daily_requests": "Daily fetch attempts",
                  "min_score": "Minimum page priority", "search_enabled": "Use Brave web search"}
        help_texts = {
            "sources": "Choose reviewed sources. Campaigns have their own schedule; the source's regular schedule can stay off. Pausing a source also pauses its campaigns.",
            "keywords": "One industry phrase per line. Used to prioritize pages, not to label a person's services.",
            "sales_terms": "One sales role or business role per line.",
            "exclusions": "One rule per line. path:blog matches blog/blogs in the URL path only; domain:x.com matches that domain and subdomains. Plain phrases match path, hostname, link label or local context. Filters never grant source approval.",
            "min_score": "After reviewing exclusions, try 35 for a small contact-focused run. Reviewed starting pages are still visited; other page URLs must meet this score.",
            "region": "Optional ranking hint, not verified geography. Add the location to search queries for search targeting.",
            "use_sitemaps": "Read bounded XML sitemaps only within each source's approved scope.",
            "search_enabled": "Requires server configuration and an API key. Off by default; normal discovery needs no key.",
            "max_depth": "Page link depth from a starting source; sitemap entries start at depth 1.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sources"].queryset = Source.objects.filter(approved=True).order_by("name")
        self.fields["sources"].label_from_instance = lambda s: f"{s.name} — {s.url}"

    def clean(self):
        data = super().clean()
        if data.get("add_contact_exclusions"):
            data["exclusions"] = "\n".join(dict.fromkeys(lines(data.get("exclusions", "")) + list(CONTACT_EXCLUSIONS)))
            if len(data["exclusions"]) > 3000:
                self.add_error("exclusions", "Combined exclusions must be at most 3000 characters.")
        for rule in lines(data.get("exclusions", "")):
            if rule.startswith(("path:", "domain:")) and not rule.split(":", 1)[1].strip():
                self.add_error("exclusions", "Each path: or domain: rule needs a value.")
            if rule.startswith("domain:"):
                domain = rule[7:].strip()
                if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+\.?", domain):
                    self.add_error("exclusions", "Use a hostname after domain:, such as domain:linkedin.com.")
        if not data.get("sources") and not data.get("search_enabled"):
            self.add_error("sources", "Select at least one approved source, or enable configured search.")
        queries = [q.strip() for q in data.get("search_queries", "").splitlines() if q.strip()]
        if len(queries) > 10 or any(len(q) > 600 or len(q.split()) > 75 for q in queries):
            self.add_error("search_queries", "Use up to 10 queries; each must be at most 600 characters and 75 words.")
        if data.get("search_enabled"):
            if not search_ready():
                self.add_error("search_enabled", "Brave search must be enabled with an API key in the server environment first.")
            if not queries:
                self.add_error("search_queries", "Add at least one query.")
        return data


class ApproveURLForm(forms.Form):
    source = forms.ModelChoiceField(queryset=Source.objects.none(), label="Reviewed source covering this URL")

    def __init__(self, candidate, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.candidate = candidate
        ids = [s.pk for s in Source.objects.filter(approved=True) if in_scope(s, candidate.url)]
        self.fields["source"].queryset = Source.objects.filter(pk__in=ids)
        self.fields["source"].label_from_instance = lambda s: f"{s.name} — {s.allowed_paths}"
