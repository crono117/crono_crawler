from django.urls import path
from . import bulk_views, views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("sources/", views.sources, name="sources"),
    path("sources/import/", bulk_views.source_import, name="source_import"),
    path("sources/import/example/", bulk_views.source_import_download, {"kind": "example"}, name="source_import_example"),
    path("sources/import/schema/", bulk_views.source_import_download, {"kind": "schema"}, name="source_import_schema"),
    path("sources/import/guide/", bulk_views.source_import_download, {"kind": "guide"}, name="source_import_guide"),
    path("sources/new/", views.source_form, name="source_new"),
    path("sources/<int:pk>/", views.source_detail, name="source_detail"),
    path("sources/<int:pk>/edit/", views.source_form, name="source_edit"),
    path("sources/<int:pk>/action/", views.source_action, name="source_action"),
    path("leads/", views.lead_list, name="leads"),
    path("leads/export/", views.export_leads, name="export"),
    path("leads/<int:pk>/", views.lead_detail, name="lead_detail"),
    path("runs/", views.runs, name="runs"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("discovery/legacy/", views.candidates, name="candidates"),
    path("discovery/<int:pk>/dismiss/", views.candidate_dismiss, name="candidate_dismiss"),
]
