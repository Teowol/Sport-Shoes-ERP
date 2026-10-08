from django.contrib import admin

from .models import Employee, ModuleLink


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("code", "full_name", "profession", "department", "is_active")
    search_fields = ("code", "full_name", "profession", "department")
    list_filter = ("is_active", "department")


@admin.register(ModuleLink)
class ModuleLinkAdmin(admin.ModelAdmin):
    list_display = ("label", "route", "group", "icon", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    list_filter = ("group", "is_active")
    search_fields = ("label", "route")
    fields = ("label", "route", "icon", "group", "sort_order", "is_active")
