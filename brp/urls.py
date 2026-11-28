from django.urls import path
from . import views

app_name = "brp"

urlpatterns = [
    path("", views.brp_dashboard, name="dashboard"),
    path("course/add/", views.add_course, name="add_course"),
    path("course/<int:course_id>/delete/", views.delete_course, name="delete_course"),
    path("course/<int:course_id>/component/add/", views.add_component, name="add_component"),
    path("course/<int:course_id>/parse/", views.parse_brp_upload, name="parse_upload"),
    path("component/<int:comp_id>/edit/", views.edit_component, name="edit_component"),
    path("component/<int:comp_id>/delete/", views.delete_component, name="delete_component"),
    path("demo-seed/", views.load_demo_brp, name="load_demo"),
]