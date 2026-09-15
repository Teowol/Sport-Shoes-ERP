from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils.html import format_html

from .forms import DocumentAdminForm
from .models import Document
from .permissions import can_manage_documents
from .signals import dispatch_document_processing


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    form = DocumentAdminForm
    list_display = (
        "title",
        "original_filename",
        "status",
        "formatted_file_size",
        "uploaded_by",
        "created_at",
    )
    list_filter = ("status", "mime_type", "created_at")
    search_fields = ("title", "original_filename", "checksum_sha256")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    actions = ("queue_for_reprocessing",)

    @admin.action(description="Seçili dokümanları yeniden işle")
    def queue_for_reprocessing(self, request, queryset):
        if not can_manage_documents(request.user):
            raise PermissionDenied
        public_ids = list(
            queryset.exclude(status=Document.Status.PROCESSING).values_list(
                "public_id", flat=True
            )
        )
        if not public_ids:
            self.message_user(request, "İşleme alınabilecek doküman bulunamadı.")
            return
        Document.objects.filter(public_id__in=public_ids).update(
            status=Document.Status.QUEUED,
            processing_error="",
        )
        dispatch_document_processing(public_ids)
        self.message_user(
            request,
            f"{len(public_ids)} doküman işleme kuyruğuna alındı.",
        )

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("title", "file")
        return (
            "title",
            "protected_download",
            "original_filename",
            "mime_type",
            "formatted_file_size",
            "checksum_sha256",
            "status",
            "processing_error",
            "uploaded_by",
            "created_at",
            "updated_at",
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return (
            "protected_download",
            "original_filename",
            "mime_type",
            "formatted_file_size",
            "checksum_sha256",
            "status",
            "processing_error",
            "uploaded_by",
            "created_at",
            "updated_at",
        )

    @admin.display(description="Dosya Boyutu")
    def formatted_file_size(self, obj):
        if not obj or obj.file_size is None:
            return "-"
        return f"{obj.file_size / 1024:.1f} KiB"

    @admin.display(description="Korumalı İndirme")
    def protected_download(self, obj):
        if not obj or not obj.pk:
            return "-"
        url = reverse("ai:document_download", kwargs={"public_id": obj.public_id})
        return format_html('<a href="{}">Dosyayı indir</a>', url)

    def save_model(self, request, obj, form, change):
        if not can_manage_documents(request.user):
            raise PermissionDenied
        if not change:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset if can_manage_documents(request.user) else queryset.none()

    def has_module_permission(self, request):
        return can_manage_documents(request.user)

    def has_view_permission(self, request, obj=None):
        return can_manage_documents(request.user)

    def has_add_permission(self, request):
        return can_manage_documents(request.user)

    def has_change_permission(self, request, obj=None):
        return can_manage_documents(request.user)

    def has_delete_permission(self, request, obj=None):
        return can_manage_documents(request.user)
