from django.urls import path
from . import api, views

app_name = "automation"
urlpatterns = [
    path("", views.home, name="home"),
    path("policies/<int:campaign_id>/", views.policy, name="policy"),
    path("jobs/<int:pk>/", views.job_detail, name="job"),
    path("jobs/<int:pk>/action/", views.job_action, name="action"),
    path("setup/<int:campaign_id>/<int:source_id>/", views.source_setup, name="setup"),
    path("api/jobs/", api.jobs),
    path("api/jobs/<int:pk>/recon/", api.recon),
    path("api/jobs/<int:pk>/bundle/", api.bundle),
    path("api/jobs/<int:pk>/candidate/", api.candidate),
]
