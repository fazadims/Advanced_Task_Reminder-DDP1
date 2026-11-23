from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render


class IndonesianUserCreationForm(UserCreationForm):
    """UserCreationForm with Bahasa Indonesia labels and validation messages."""

    error_messages = {
        "password_mismatch": "Konfirmasi kata sandi tidak cocok.",
    }

    class Meta(UserCreationForm.Meta):
        error_messages = {
            "username": {
                "unique": "Nama pengguna ini sudah digunakan.",
            },
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Nama Pengguna"
        self.fields["username"].error_messages.update(
            {
                "required": "Nama pengguna wajib diisi.",
                "unique": "Nama pengguna ini sudah digunakan.",
                "invalid": "Nama pengguna hanya boleh berisi huruf, angka, dan karakter @/./+/-/_.",
            }
        )
        self.fields["password1"].label = "Kata Sandi"
        self.fields["password1"].error_messages.update(
            {
                "required": "Kata sandi wajib diisi.",
            }
        )
        self.fields["password2"].label = "Konfirmasi Kata Sandi"
        self.fields["password2"].error_messages.update(
            {
                "required": "Konfirmasi kata sandi wajib diisi.",
            }
        )

    def _post_clean(self):
        super()._post_clean()
        password_errors = self._errors.get("password2")
        if not password_errors:
            return
        code_map = {
            "password_too_short": "Kata sandi terlalu pendek. Minimal harus 8 karakter.",
            "password_too_similar": "Kata sandi terlalu mirip dengan nama pengguna atau data akunmu.",
            "password_too_common": "Kata sandi ini terlalu umum dipakai. Pilih kombinasi yang lebih unik.",
            "password_entirely_numeric": "Kata sandi tidak boleh hanya terdiri dari angka.",
        }
        translated = []
        for err in password_errors.as_data():
            if err.code in code_map:
                err.message = code_map[err.code]
            translated.append(err)
        self._errors["password2"] = self.error_class(translated)



def register_view(request):
    """Minimal F0 user registration view so Person B can test @login_required features locally."""
    if request.user.is_authenticated:
        return redirect("tasks:task_list")
    if request.method == "POST":
        form = IndonesianUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("tasks:task_list")
    else:
        form = IndonesianUserCreationForm()
    return render(request, "registration/register.html", {"form": form})
