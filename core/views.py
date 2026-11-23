from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render


def register_view(request):
    """Minimal F0 user registration view so Person B can test @login_required features locally."""
    if request.user.is_authenticated:
        return redirect("tasks:task_list")
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("tasks:task_list")
    else:
        form = UserCreationForm()
    return render(request, "registration/register.html", {"form": form})
