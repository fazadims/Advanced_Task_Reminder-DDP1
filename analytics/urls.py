from django.urls import path

from . import views

app_name = "analytics"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("task/<int:pk>/", views.task_detail, name="task_detail"),
]
