from django.contrib import admin

from brp.models import Course, GradingComponent


class GradingComponentInline(admin.TabularInline):
    model = GradingComponent
    extra = 1


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "user", "credits", "semester", "is_demo")
    list_filter = ("semester", "is_demo")
    search_fields = ("code", "name")
    inlines = [GradingComponentInline]
