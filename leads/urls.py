from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("sources/", views.sources, name="sources"),
    path("sources/new/", views.source_form, name="source_new"),
    path("sources/<int:pk>/", views.source_detail, name="source_detail"),
    path("sources/<int:pk>/edit/", views.source_form, name="source_edit"),
    path("sources/<int:pk>/action/", views.source_action, name="source_action"),
    path("leads/", views.lead_list, name="leads"),
    path("leads/export/", views.export_leads, name="export"),
    path("leads/<int:pk>/", views.lead_detail, name="lead_detail"),
    path("runs/", views.runs, name="runs"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("discovery/", views.candidates, name="candidates"),
    path("discovery/<int:pk>/dismiss/", views.candidate_dismiss, name="candidate_dismiss"),
]
