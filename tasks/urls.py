from django.urls import path

from tasks import views

app_name = "tasks"

urlpatterns = [
    path("", views.task_list, name="task_list"),
    path("new/", views.task_create, name="task_create"),
    path("history/", views.task_history, name="task_history"),
    path("<int:pk>/", views.task_detail, name="task_detail"),
    path("<int:pk>/edit/", views.task_update, name="task_update"),
    path("<int:pk>/delete/", views.task_delete, name="task_delete"),
    path("<int:pk>/archive/", views.task_archive, name="task_archive"),
    # Real-time Checkpoints (F3.1 & F3.2)
    path("<int:task_pk>/checkpoints/add/", views.add_checkpoint, name="add_checkpoint"),
    path("checkpoints/<int:pk>/toggle/", views.toggle_checkpoint, name="toggle_checkpoint"),
    path("checkpoints/<int:pk>/edit/", views.edit_checkpoint, name="edit_checkpoint"),
    path("checkpoints/<int:pk>/delete/", views.delete_checkpoint, name="delete_checkpoint"),
    path("<int:pk>/checkpoints/generate/", views.regenerate_checkpoints, name="regenerate_checkpoints"),
    # Focus Mode & Pomodoro (F3.3)
    path("<int:pk>/focus/", views.focus_mode_view, name="focus_mode"),
    path("focus/start/", views.start_focus_session, name="start_focus_session"),
    path("focus/<int:session_id>/finish/", views.finish_focus_session, name="finish_focus_session"),
    # Optional AI Sub-task Suggestions with Fallback (F3.4)
    path("<int:pk>/ai-checkpoints/", views.preview_ai_checkpoints, name="preview_ai_checkpoints"),
    path("<int:pk>/ai-checkpoints/apply/", views.apply_ai_checkpoints, name="apply_ai_checkpoints"),
]
