from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError

from brp.models import Course, GradingComponent
from brp.services.parser import extract_text_from_pdf, parse_brp_components
from core.services.demo_seed import seed_demo_courses_and_brp


@login_required
def brp_dashboard(request):
    courses = Course.objects.filter(user=request.user).prefetch_related("components")
    context = {
        "courses": courses,
        "categories": GradingComponent.CATEGORY_CHOICES,
    }
    return render(request, "brp/index.html", context)


@login_required
def add_course(request):
    if request.method == "POST":
        code = request.POST.get("code", "").strip().upper()
        name = request.POST.get("name", "").strip()
        sks = int(request.POST.get("sks", 3) or 3)
        semester = request.POST.get("semester", "Gasal 2026/2027").strip()

        if not code or not name:
            messages.error(request, "Kode dan nama mata kuliah wajib diisi.")
            return redirect("brp:dashboard")

        course, created = Course.objects.get_or_create(
            user=request.user,
            code=code,
            defaults={"name": name, "sks": sks, "semester": semester},
        )
        if created:
            messages.success(request, f"Mata kuliah {course.name} berhasil ditambahkan.")
        else:
            messages.warning(request, f"Mata kuliah dengan kode {code} sudah ada.")

    return redirect("brp:dashboard")


@login_required
def add_component(request, course_id):
    course = get_object_or_404(Course, id=course_id, user=request.user)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        category = request.POST.get("category", "TP")
        weight = float(request.POST.get("weight_percentage", 0) or 0)
        item_count = int(request.POST.get("item_count", 1) or 1)

        comp = GradingComponent(
            course=course,
            name=name,
            category=category,
            weight_percentage=weight,
            item_count=max(1, item_count),
        )
        try:
            comp.full_clean()
            comp.save()
            messages.success(request, f"Komponen '{name}' ({weight}%) ditambahkan.")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))

    return redirect("brp:dashboard")


@login_required
def parse_brp_upload(request, course_id):
    course = get_object_or_404(Course, id=course_id, user=request.user)
    if request.method == "POST":
        raw_text = request.POST.get("raw_text", "").strip()
        pdf_file = request.FILES.get("pdf_file")

        extracted_text = ""
        if pdf_file:
            try:
                extracted_text = extract_text_from_pdf(pdf_file)
            except Exception as e:
                messages.error(request, f"Gagal membaca file PDF: {e}")
                return redirect("brp:dashboard")
        elif raw_text:
            extracted_text = raw_text

        if not extracted_text:
            messages.error(request, "Unggah file PDF BRP atau tempelkan teks komponen penilaian.")
            return redirect("brp:dashboard")

        parsed_items = parse_brp_components(extracted_text)
        if not parsed_items:
            messages.warning(request, "Tidak ditemukan pola bobot persentase (contoh: 'UTS: 30%').")
            return redirect("brp:dashboard")

        added_count = 0
        for item in parsed_items:
            comp = GradingComponent(course=course, **item)
            try:
                comp.full_clean()
                comp.save()
                added_count += 1
            except ValidationError:
                continue

        messages.success(request, f"Berhasil mengekstrak {added_count} komponen penilaian dari BRP!")

    return redirect("brp:dashboard")


@login_required
def delete_component(request, comp_id):
    comp = get_object_or_404(GradingComponent, id=comp_id, course__user=request.user)
    if request.method == "POST":
        comp.delete()
        messages.info(request, f"Komponen '{comp.name}' dihapus.")
    return redirect("brp:dashboard")


@login_required
def load_demo_brp(request):
    if request.method == "POST":
        seed_demo_courses_and_brp(request.user)
        messages.success(request, "Data demo mata kuliah Fasilkom & bobot BRP berhasil dimuat!")
    return redirect("brp:dashboard")

@login_required
def edit_component(request, comp_id):
    comp = get_object_or_404(GradingComponent, id=comp_id, course__user=request.user)
    if request.method == "POST":
        comp.name = request.POST.get("name", comp.name).strip()
        comp.category = request.POST.get("category", comp.category)
        comp.weight_percentage = float(request.POST.get("weight_percentage", comp.weight_percentage) or 0)
        comp.item_count = max(1, int(request.POST.get("item_count", comp.item_count) or 1))
        try:
            comp.full_clean()
            comp.save()
            messages.success(request, f"Komponen '{comp.name}' berhasil diperbarui.")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
    return redirect("brp:dashboard")


@login_required
def delete_course(request, course_id):
    course = get_object_or_404(Course, id=course_id, user=request.user)
    if request.method == "POST":
        course_name = course.name
        course.delete()
        messages.info(request, f"Mata kuliah '{course_name}' berhasil dihapus.")
    return redirect("brp:dashboard")