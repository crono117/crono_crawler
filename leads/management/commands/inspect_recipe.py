"""Read-only recipe preview against an operator-saved HTML file. No requests or writes."""
import copy
import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from leads.models import Source
from leads.services.extraction import DEFAULT_SELECTORS, diagnostic_message, extract, validate_recipe


class Command(BaseCommand):
    help = "Check a CSS or structured recipe against saved HTML without requests, models, or contact writes."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, required=True, help="Existing source ID supplying recipe and validation settings.")
        parser.add_argument("--html", required=True, help="Locally saved HTML file (at most 6 MiB).")
        parser.add_argument("--recipe", help="Optional JSON recipe to preview without changing source settings.")
        parser.add_argument("--show-records", action="store_true", help="Print validated fields and evidence locally for review.")

    def handle(self, *args, **options):
        try:
            source = copy.copy(Source.objects.get(pk=options["source"]))
        except Source.DoesNotExist as exc:
            raise CommandError("Source ID was not found.") from exc
        try:
            with Path(options["html"]).open("rb") as handle:
                raw = handle.read(6 * 1024 * 1024 + 1)
            if len(raw) > 6 * 1024 * 1024:
                raise ValueError("HTML exceeds the 6 MiB limit.")
            if options["recipe"]:
                with Path(options["recipe"]).open(encoding="utf-8") as handle:
                    recipe_text = handle.read(16001)
                if len(recipe_text) > 16000:
                    raise ValueError("Recipe exceeds the 16000-character limit.")
                source.recipe = json.loads(recipe_text)
            validate_recipe(source.recipe or {})
            # This command explicitly tests CSS even if the stored source uses Ollama.
            source.extractor = "rules"
            diagnostics = {}
            records, page_tags = extract(raw.decode("utf-8", errors="replace"), source, diagnostics=diagnostics)
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        result = {"source_id": source.pk, "url": source.url, "mode": "offline recipe preview; no data saved",
                  "selectors": source.recipe if source.recipe.get("engine") else DEFAULT_SELECTORS | (source.recipe or {}), "diagnostics": diagnostics,
                  "summary": diagnostic_message(diagnostics), "page_tags": page_tags}
        if options["show_records"]:
            result["records"] = records
        self.stdout.write(json.dumps(result, indent=2))
