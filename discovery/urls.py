from django.urls import path
from . import views

app_name = "discovery"
urlpatterns = [
    path("", views.home, name="home"),
    path("campaigns/new/", views.campaign_form, name="new"),
    path("campaigns/<int:pk>/", views.campaign_detail, name="campaign"),
    path("campaigns/<int:pk>/edit/", views.campaign_form, name="edit"),
    path("campaigns/<int:pk>/action/", views.campaign_action, name="action"),
    path("candidates/", views.candidates, name="candidates"),
    path("candidates/<int:pk>/", views.candidate_detail, name="candidate"),
    path("runs/<int:pk>/", views.run_detail, name="run"),
]
