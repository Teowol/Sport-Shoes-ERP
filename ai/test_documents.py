import hashlib
import io
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from docx import Document as DocxDocument
from pypdf import PdfWriter

from .models import Document


PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def txt_upload(name="guide.txt", content=b"Guvenli UTF-8 dokumani", mime_type="text/plain"):
    return SimpleUploadedFile(name, content, content_type=mime_type)


def pdf_upload(name="guide.pdf", mime_type=PDF_MIME):
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return SimpleUploadedFile(name, output.getvalue(), content_type=mime_type)


def docx_upload(name="guide.docx", mime_type=DOCX_MIME):
    output = io.BytesIO()
    document = DocxDocument()
    document.add_paragraph("Guvenli dokuman")
    document.save(output)
    return SimpleUploadedFile(name, output.getvalue(), content_type=mime_type)


class DocumentTestBase(TestCase):
    def setUp(self):
        super().setUp()
        self.media_directory = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.settings_override.enable()

        user_model = get_user_model()
        self.staff = user_model.objects.create(username="doc-staff", is_staff=True)
        self.superuser = user_model.objects.create(
            username="doc-superuser", is_staff=True, is_superuser=True
        )
        self.buyer_staff = user_model.objects.create(username="doc-buyer", is_staff=True)
        self.factory_user = user_model.objects.create(username="doc-factory")
        Group.objects.create(name="Buyer").user_set.add(self.buyer_staff)
        Group.objects.create(name="FactoryOwner").user_set.add(self.factory_user)

    def tearDown(self):
        self.settings_override.disable()
        self.media_directory.cleanup()
        super().tearDown()

    def create_document(self, upload=None, user=None, title="Prosedür"):
        return Document.objects.create(
            title=title,
            file=upload or txt_upload(),
            uploaded_by=user or self.staff,
        )


class DocumentValidationTests(DocumentTestBase):
    def test_pdf_docx_and_utf8_txt_are_accepted(self):
        uploads = (pdf_upload(), docx_upload(), txt_upload(content="Türkçe içerik".encode()))

        for index, upload in enumerate(uploads):
            with self.subTest(filename=upload.name):
                document = self.create_document(upload=upload, title=f"Belge {index}")
                self.assertEqual(document.status, Document.Status.QUEUED)
                self.assertEqual(document.processing_error, "")
                self.assertTrue(document.file.storage.exists(document.file.name))

    def test_checksum_and_uuid_storage_name_ignore_untrusted_path(self):
        content = b"checksum content"
        document = self.create_document(
            upload=txt_upload(name="..\\..\\private plan.txt", content=content)
        )

        self.assertEqual(document.checksum_sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(document.original_filename, "private_plan.txt")
        self.assertIn(str(document.public_id), document.file.name)
        self.assertNotIn("private_plan", document.file.name)
        self.assertNotIn("..", document.file.name)

    def test_extension_mime_and_content_signature_are_all_checked(self):
        invalid_uploads = (
            txt_upload(name="guide.exe"),
            txt_upload(name="guide.pdf", mime_type=PDF_MIME),
            txt_upload(name="guide.pdf", mime_type="text/plain"),
            txt_upload(name="guide.txt", content=b"\xff\xfe", mime_type="text/plain"),
            txt_upload(name="guide.txt", content=b"text\x00binary", mime_type="text/plain"),
            SimpleUploadedFile("guide.docx", b"PK\x03\x04not-a-docx", content_type=DOCX_MIME),
        )

        for upload in invalid_uploads:
            with self.subTest(filename=upload.name, mime=upload.content_type):
                with self.assertRaises(ValidationError):
                    self.create_document(upload=upload)

        self.assertEqual(Document.objects.count(), 0)

    @override_settings(AI_DOCUMENT_MAX_SIZE_BYTES=4)
    def test_file_size_limit_is_enforced_before_storage(self):
        with self.assertRaises(ValidationError):
            self.create_document(upload=txt_upload(content=b"12345"))

        self.assertEqual(Document.objects.count(), 0)


class DocumentAccessTests(DocumentTestBase):
    def test_staff_can_upload_through_admin_and_uploader_is_server_assigned(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("admin:ai_document_add"),
            {"title": "Admin Belgesi", "file": txt_upload()},
        )

        self.assertEqual(response.status_code, 302)
        document = Document.objects.get(title="Admin Belgesi")
        self.assertEqual(document.uploaded_by, self.staff)

    def test_buyer_is_blocked_from_document_admin_even_when_marked_staff(self):
        self.client.force_login(self.buyer_staff)
        response = self.client.post(
            reverse("admin:ai_document_add"),
            {"title": "Yasak Belge", "file": txt_upload()},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Document.objects.filter(title="Yasak Belge").exists())

    def test_factory_staff_and_superuser_can_download_but_buyer_cannot(self):
        document = self.create_document()
        download_url = reverse("ai:document_download", kwargs={"public_id": document.public_id})

        self.client.force_login(self.staff)
        staff_response = self.client.get(download_url)
        self.assertEqual(staff_response.status_code, 200)
        self.assertEqual(b"".join(staff_response.streaming_content), b"Guvenli UTF-8 dokumani")
        self.assertEqual(staff_response["Cache-Control"], "private, no-store")
        self.assertEqual(staff_response["X-Content-Type-Options"], "nosniff")

        self.client.force_login(self.superuser)
        superuser_response = self.client.get(download_url)
        self.assertEqual(superuser_response.status_code, 200)
        self.assertEqual(b"".join(superuser_response.streaming_content), b"Guvenli UTF-8 dokumani")

        self.client.force_login(self.factory_user)
        factory_response = self.client.get(download_url)
        self.assertEqual(factory_response.status_code, 200)
        self.assertEqual(b"".join(factory_response.streaming_content), b"Guvenli UTF-8 dokumani")

        self.client.force_login(self.buyer_staff)
        self.assertEqual(self.client.get(download_url).status_code, 403)

        self.client.logout()
        self.assertEqual(self.client.get(download_url).status_code, 302)

    def test_admin_change_page_uses_protected_link_not_media_url(self):
        document = self.create_document()
        self.client.force_login(self.staff)

        response = self.client.get(reverse("admin:ai_document_change", args=[document.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("ai:document_download", kwargs={"public_id": document.public_id}),
        )
        self.assertNotContains(response, f"/media/{document.file.name}")
        self.assertEqual(self.client.get(f"/media/{document.file.name}").status_code, 404)
