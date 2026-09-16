from django import forms
from django.conf import settings
from .models import Lead, Source
from .services.network import canonical_url, in_scope, public_addresses
from .services.extraction import validate_recipe
from urllib.parse import urlsplit

class SourceForm(forms.ModelForm):
    class Meta:
        model = Source
        fields = ["name", "url", "company", "category", "collector", "extractor", "require_sales_role", "recipe", "setup_mode",
                  "allowed_paths", "allow_homepage", "follow_links", "discover_external", "interval_hours", "delay_seconds",
                  "max_pages", "max_depth", "approved", "approval_notes"]
        labels = {"category": "Source business type", "recipe": "CSS recipe (optional JSON)",
                  "approved": "I have reviewed this source for collection", "require_sales_role": "Require a sales-related role or description",
                  "discover_external": "Save external websites as review candidates"}
        widgets = {"recipe": forms.Textarea(attrs={"rows": 8, "placeholder": '{"row": ".team-member", "name": "h3"}'}),
                   "approval_notes": forms.Textarea(attrs={"rows": 3}), "allowed_paths": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"company": "Optional source company name. This is your supplied context, not an extracted fact.",
                      "approval_notes": "Record why collection is appropriate, any source terms, and restrictions.",
                      "recipe": "Leave blank for common team cards. Field selectors are relative to each person row. Optional evidence selects the row itself or a smaller container inside it; it must contain the name and contact evidence. Never use a page-wide footer contact as a person's contact.",
                      "setup_mode": "Operator-configured rules use this recipe directly. Automated setup runs through a campaign policy, validation and a canary first.",
                      "discover_external": "Candidates are saved for review; the collector does not fetch them automatically.",
                      "require_sales_role": "Turn off only when the selected page is already a curated directory of relevant representatives."}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["setup_mode"].required = False
        self.fields["collector"].choices = [("http", "HTML"), ("browser", "Browser")]
        self.fields["interval_hours"].min_value = 1
        self.fields["interval_hours"].max_value = 8760
        self.fields["delay_seconds"].min_value = 2
        self.fields["delay_seconds"].max_value = 3600
        self.fields["max_pages"].min_value = 1
        self.fields["max_pages"].max_value = 500
        self.fields["max_depth"].max_value = 5
        self.fields["max_depth"].min_value = 0
    def clean_url(self):
        try:
            url = canonical_url(self.cleaned_data["url"])
            p = urlsplit(url)
            public_addresses(p.hostname, 443 if p.scheme == "https" else 80)
            return url
        except Exception as exc:
            raise forms.ValidationError(str(exc)) from exc
    def clean_allowed_paths(self):
        paths = [p.strip() for p in self.cleaned_data["allowed_paths"].splitlines() if p.strip()]
        if not paths or any(not p.startswith("/") or ".." in p or "?" in p or "\\" in p for p in paths):
            raise forms.ValidationError("Use one path prefix per line, beginning with /. No query strings or traversal segments.")
        return "\n".join(paths)
    def clean_recipe(self):
        recipe = self.cleaned_data.get("recipe") or {}
        try:
            return validate_recipe(recipe)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
    def clean(self):
        data = super().clean()
        data["setup_mode"] = data.get("setup_mode") or self.instance.setup_mode or "rules_only"
        for field, bounds in {"interval_hours": (1, 8760), "delay_seconds": (2, 3600), "max_pages": (1, 500), "max_depth": (0, 5)}.items():
            value = data.get(field)
            if value is not None and not bounds[0] <= value <= bounds[1]:
                self.add_error(field, f"Use a value from {bounds[0]} to {bounds[1]}.")
        if data.get("extractor") == "ollama" and not settings.OLLAMA_MODEL:
            self.add_error("extractor", "Configure OLLAMA_MODEL in .env before choosing local AI.")
        if data.get("url") and data.get("allowed_paths"):
            candidate = Source(url=data["url"], allowed_paths=data["allowed_paths"], allow_homepage=data.get("allow_homepage", False))
            if not in_scope(candidate, candidate.url):
                self.add_error("allowed_paths", "The starting URL must be within an allowed path.")
        if data.get("approved") and not data.get("approval_notes", "").strip():
            self.add_error("approval_notes", "Add a short source review note before approving collection.")
        return data

class LeadReviewForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ["status", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 5})}
