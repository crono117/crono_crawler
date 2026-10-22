from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from .auth import LimitedAuthenticationForm

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            authentication_form=LimitedAuthenticationForm,
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
    path("discovery/", include("discovery.urls")),
    path("automation/", include("automation.urls")),
    path("classification/", include("classification.urls")),
    path("", include("leads.urls")),
]
