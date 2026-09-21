from django.urls import path
from . import views

app_name = 'classification'
urlpatterns = [
    path('', views.home, name='home'),
    path('evaluations/<uuid:pk>/', views.detail, name='detail'),
    path('control/', views.control, name='control'),
    path('domains/<int:pk>/verify/', views.domain, name='domain'),
    path('judgments/<int:pk>/review/', views.review, name='review'),
]
