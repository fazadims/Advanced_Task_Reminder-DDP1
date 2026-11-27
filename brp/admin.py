from django.contrib import admin
from .models import Course, GradingComponent


class GradingComponentInline(admin.TabularInline):
    model = GradingComponent
    extra = 1


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "user", "sks", "total_weight", "is_weight_valid")
    inlines = [GradingComponentInline]