"""Staff-only, two-step browser import of inert Source settings."""
from django.conf import settings
from django.core import signing
from django.http import FileResponse, Http404
from django.db import DatabaseError
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .services.source_import import MAX_BYTES, import_sources, parse_upload
from .views import staff_required

SIGNING_SALT = "leads.bulk-source-import.v1"
TOKEN_MAX_AGE = 900
MAX_TOKEN_LENGTH = 524288
TEMPLATE = "leads/source_import.html"
DOWNLOADS = {
    "example": ("examples/bulk-sources.json", "application/json"),
    "schema": ("examples/bulk-sources.schema.json", "application/json"),
    "guide": ("docs/BULK_SOURCE_IMPORT.md", "text/markdown"),
}


@staff_required
@require_http_methods(["GET"])
def source_import_download(request, kind):
    relative, content_type = DOWNLOADS[kind]  # Fixed URLconf arguments, never request paths.
    path = settings.BASE_DIR / relative
    try:
        handle = path.open("rb")
    except OSError:
        raise Http404("Import reference file is unavailable.") from None
    return FileResponse(handle, as_attachment=True, filename=path.name, content_type=content_type)


@staff_required
@require_http_methods(["GET", "POST"])
def source_import(request):
    if request.method == "GET":
        return render(request, TEMPLATE)
    try:
        action = request.POST.get("action")
        if action == "preview":
            upload = request.FILES.get("file")
            if upload is None or not upload.name.lower().endswith(".json"):
                raise ValueError("Choose a .json file to upload.")
            if upload.size > MAX_BYTES:
                raise ValueError("Upload a JSON file no larger than 256 KiB.")
            document = parse_upload(upload.read(MAX_BYTES + 1))
            report = import_sources(document)
            token = signing.dumps({"user_id": request.user.pk, "document": document}, salt=SIGNING_SALT, compress=True)
            if len(token) > MAX_TOKEN_LENGTH:
                raise ValueError("Preview data is too large; use a smaller file.")
            return render(request, TEMPLATE, {"report": report, "preview_token": token})
        if action == "import":
            token = request.POST.get("preview_token", "")
            try:
                if not token or len(token) > MAX_TOKEN_LENGTH:
                    raise signing.BadSignature
                payload = signing.loads(token, salt=SIGNING_SALT, max_age=TOKEN_MAX_AGE)
                if not isinstance(payload, dict) or set(payload) != {"user_id", "document"} or payload["user_id"] != request.user.pk:
                    raise signing.BadSignature
            except (signing.BadSignature, ValueError, TypeError):
                raise ValueError("This preview is invalid, expired or belongs to another account. Please upload the file again.") from None
            # Signed data is still revalidated and current duplicate decisions recomputed.
            report = import_sources(payload["document"], apply=True)
            return render(request, TEMPLATE, {"report": report, "imported": True})
        raise ValueError("Choose Preview or confirm a signed preview to import.")
    except ValueError as exc:
        return render(request, TEMPLATE, {"error": str(exc)}, status=400)
    except DatabaseError:
        # The service's atomic block rolls back every source on a database failure.
        return render(request, TEMPLATE, {"error": "The import could not be saved. No partial batch was saved. Please upload and preview again."}, status=400)
