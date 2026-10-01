from django.contrib import admin

from .models import Employee


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("code", "full_name", "profession", "department", "is_active")
    search_fields = ("code", "full_name", "profession", "department")
    list_filter = ("is_active", "department")
