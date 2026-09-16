import re
from django import forms
from .models import SitePolicy
from leads.services.extraction import validate_recipe


class PolicyForm(forms.ModelForm):
    class Meta:
        model = SitePolicy
        fields = ["enabled", "min_url_score", "allowed_paths", "allow_homepage", "denied_domains", "max_sites_per_day",
                  "probe_pages", "canary_pages", "delay_seconds", "min_recipe_score", "recheck_days", "notes"]
        labels = {"enabled": "Authorize automatic setup for qualifying public business sites",
                  "min_url_score": "Minimum discovery score for automatic setup",
                  "min_recipe_score": "Minimum recipe readiness score",
                  "allow_homepage": "Also allow the exact homepage"}
        widgets = {key: forms.Textarea(attrs={"rows": 5}) for key in ("allowed_paths", "denied_domains", "notes")}
        help_texts = {
            "enabled": "Approve this campaign policy once. Matching new sites can be probed, tested and released automatically; exceptions appear in Site automation.",
            "allowed_paths": "One allowed path prefix per line. Applied to new sites. / grants the whole origin; crawling still follows relevance scores and page limits.",
            "allow_homepage": "Allows / without granting all other paths. Redirects must stay in the exact approved origin and scope.",
            "denied_domains": "Additional hostnames to exclude. Social platforms remain excluded from automatic setup.",
            "min_recipe_score": "85 permits a supported individual profile; 90 requires repeated cards. This readiness score is not a probability. Published evidence and a passing canary are always required.",
            "notes": "Your operating policy and any source restrictions. Policy authorization is recorded separately from an individual human source review.",
            "delay_seconds": "Minimum delay for newly authorized sites. Robots rules may require a longer delay.",
        }

    def clean_allowed_paths(self):
        paths = list(dict.fromkeys(line.strip() for line in self.cleaned_data["allowed_paths"].splitlines() if line.strip()))
        if not paths or any(not p.startswith("/") or any(c in p for c in ("..", "?", "#", "\\")) for p in paths):
            raise forms.ValidationError("Use path prefixes starting with /; no traversal, queries or fragments.")
        return "\n".join(paths)

    def clean_denied_domains(self):
        domains = [line.strip().lower().rstrip(".") for line in self.cleaned_data["denied_domains"].splitlines() if line.strip()]
        if any(not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain) for domain in domains):
            raise forms.ValidationError("Use hostnames only, such as example.com.")
        return "\n".join(dict.fromkeys(domains))


class CandidateForm(forms.Form):
    recipe = forms.JSONField(widget=forms.Textarea(attrs={"rows": 10}), label="Candidate CSS recipe")

    def clean_recipe(self):
        try:
            recipe = validate_recipe(self.cleaned_data["recipe"])
            if any(len(value) > 500 for value in recipe.values()):
                raise ValueError("Each selector must be at most 500 characters.")
            return recipe
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
