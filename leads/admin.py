from django.contrib import admin
from .models import Lead, Observation, Run, Source, SourceCandidate
from .services.storage import record_review

@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "status", "last_seen")
    list_filter = ("status",)
    search_fields = ("name", "company", "email")
    readonly_fields = ("identity", "status_origin", "held_by", "status_decided_at", "first_seen", "last_seen")

    def save_model(self, request, obj, form, change):
        record_review(obj, "status" in form.changed_data)

# Sources and collection history are read-only here: use the validated console controls.
class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

for model in (Source, Observation, Run, SourceCandidate):
    admin.site.register(model, ReadOnlyAdmin)
admin.site.site_header = "Lead Console administration"
admin.site.site_title = "Lead Console admin"
