from django.contrib import admin
from leads.admin import ReadOnlyAdmin
from .models import AutomationEvent, CoordinatorClient, ProbePage, RecipeVersion, SiteAutomationJob, SitePolicy

for model in (AutomationEvent, ProbePage, RecipeVersion, SiteAutomationJob, SitePolicy):
    admin.site.register(model, ReadOnlyAdmin)


@admin.register(CoordinatorClient)
class CoordinatorClientAdmin(admin.ModelAdmin):
    list_display = ("name", "campaign", "active", "created_at")
    readonly_fields = ("name", "campaign", "token_hash", "created_at")
    def has_add_permission(self, request):
        return False
