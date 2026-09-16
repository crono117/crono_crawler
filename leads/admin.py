from django.contrib import admin
from .models import Lead, Observation, Run, Source, SourceCandidate

@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "status", "last_seen")
    list_filter = ("status",)
    search_fields = ("name", "company", "email")
    readonly_fields = ("identity", "first_seen", "last_seen")

# Sources and collection history are read-only here: use the validated console controls.
class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

for model in (Source, Observation, Run, SourceCandidate):
    admin.site.register(model, ReadOnlyAdmin)
admin.site.site_header = "ClearPay administration"
