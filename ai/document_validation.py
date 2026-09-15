"""Strict validation for documents uploaded to the private AI knowledge store."""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.text import get_valid_filename
from docx import Document as DocxDocument
from pypdf import PdfReader


ALLOWED_DOCUMENT_TYPES = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    },
    ".txt": {"text/plain"},
}
DOCX_REQUIRED_MEMBERS = {"[Content_Types].xml", "word/document.xml"}
DOCX_FORBIDDEN_MEMBER_SUFFIXES = ("vbaproject.bin", "vbadata.xml")
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 100
MIN_DOCX_UNCOMPRESSED_ALLOWANCE = 5 * 1024 * 1024


@dataclass(frozen=True)
class ValidatedDocument:
    original_filename: str
    extension: str
    mime_type: str
    file_size: int
    checksum_sha256: str


def _safe_original_filename(name: str) -> str:
    # Treat both POSIX and Windows separators as untrusted path separators.
    basename = PurePosixPath((name or "").replace("\\", "/")).name
    safe_name = get_valid_filename(basename)[:255]
    if not safe_name or safe_name in {".", ".."}:
        raise ValidationError("Geçerli bir dosya adı zorunludur.")
    return safe_name


def _rewind(upload) -> None:
    try:
        upload.seek(0)
    except (AttributeError, OSError):
        raise ValidationError("Dosya okunamıyor.") from None


def _read_bytes(upload, amount: int | None = None) -> bytes:
    _rewind(upload)
    try:
        content = upload.read() if amount is None else upload.read(amount)
    except (AttributeError, OSError, ValueError):
        raise ValidationError("Dosya okunamıyor.") from None
    finally:
        _rewind(upload)
    return content


def _checksum(upload) -> str:
    digest = hashlib.sha256()
    _rewind(upload)
    try:
        if hasattr(upload, "chunks"):
            for chunk in upload.chunks():
                digest.update(chunk)
        else:
            while chunk := upload.read(1024 * 1024):
                digest.update(chunk)
    except (OSError, ValueError):
        raise ValidationError("Dosya okunamıyor.") from None
    finally:
        _rewind(upload)
    return digest.hexdigest()


def _validate_pdf(upload) -> None:
    if _read_bytes(upload, 5) != b"%PDF-":
        raise ValidationError("PDF dosya imzası geçersiz.")
    try:
        _rewind(upload)
        reader = PdfReader(upload, strict=True)
        if reader.is_encrypted:
            raise ValidationError("Şifreli PDF dosyaları desteklenmiyor.")
        if len(reader.pages) < 1:
            raise ValidationError("PDF en az bir sayfa içermelidir.")
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("PDF dosyası yapısal olarak geçersiz.") from None
    finally:
        _rewind(upload)


def validate_docx_archive(archive: zipfile.ZipFile, file_size: int) -> None:
    """Reject malformed, encrypted, macro-enabled, or suspicious DOCX archives."""
    members = archive.infolist()
    member_names = {member.filename for member in members}
    if not DOCX_REQUIRED_MEMBERS.issubset(member_names):
        raise ValidationError("Dosya geçerli bir DOCX belgesi değil.")
    if any(member.flag_bits & 0x1 for member in members):
        raise ValidationError("Şifreli DOCX dosyaları desteklenmiyor.")
    if any(
        member.filename.lower().endswith(DOCX_FORBIDDEN_MEMBER_SUFFIXES)
        for member in members
    ):
        raise ValidationError("Makrolu Office dosyaları desteklenmiyor.")

    content_types = archive.read("[Content_Types].xml").lower()
    if b"macroenabled" in content_types or b"vba" in content_types:
        raise ValidationError("Makrolu Office dosyaları desteklenmiyor.")

    uncompressed_size = sum(member.file_size for member in members)
    allowed_uncompressed_size = min(
        MAX_DOCX_UNCOMPRESSED_BYTES,
        max(
            file_size * MAX_DOCX_COMPRESSION_RATIO,
            MIN_DOCX_UNCOMPRESSED_ALLOWANCE,
        ),
    )
    if uncompressed_size > allowed_uncompressed_size:
        raise ValidationError("DOCX sıkıştırılmış içeriği güvenli sınırı aşıyor.")


def _validate_docx(upload, file_size: int) -> None:
    if _read_bytes(upload, 4) not in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}:
        raise ValidationError("DOCX dosya imzası geçersiz.")
    try:
        _rewind(upload)
        with zipfile.ZipFile(upload) as archive:
            validate_docx_archive(archive, file_size)
        _rewind(upload)
        DocxDocument(upload)
    except ValidationError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError, OSError):
        raise ValidationError("DOCX dosyası yapısal olarak geçersiz.") from None
    finally:
        _rewind(upload)


def _validate_txt(upload) -> None:
    content = _read_bytes(upload)
    if b"\x00" in content:
        raise ValidationError("TXT dosyası ikili veri içeremez.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("TXT dosyası geçerli UTF-8 olmalıdır.") from None
    if not text.strip():
        raise ValidationError("TXT dosyası boş olamaz.")


def validate_document_upload(upload) -> ValidatedDocument:
    """Validate a document and return trusted metadata without saving it."""
    underlying_upload = getattr(upload, "file", upload)
    cached_result = (
        getattr(upload, "_ai_validated_document", None)
        or getattr(underlying_upload, "_ai_validated_document", None)
    )
    if cached_result is not None:
        return cached_result
    original_filename = _safe_original_filename(getattr(upload, "name", ""))
    extension = PurePosixPath(original_filename).suffix.lower()
    if extension not in ALLOWED_DOCUMENT_TYPES:
        raise ValidationError("Yalnızca PDF, DOCX ve TXT dosyaları yüklenebilir.")

    try:
        file_size = int(upload.size)
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("Dosya boyutu belirlenemedi.") from None
    max_size = int(getattr(settings, "AI_DOCUMENT_MAX_SIZE_BYTES", 10 * 1024 * 1024))
    if file_size <= 0:
        raise ValidationError("Boş dosya yüklenemez.")
    if file_size > max_size:
        raise ValidationError("Dosya izin verilen boyut sınırını aşıyor.")

    supplied_mime_type = (
        getattr(upload, "content_type", "")
        or getattr(underlying_upload, "content_type", "")
        or ""
    )
    mime_type = supplied_mime_type.split(";", 1)[0].strip().lower()
    if mime_type not in ALLOWED_DOCUMENT_TYPES[extension]:
        raise ValidationError("Dosyanın MIME türü uzantısıyla eşleşmiyor.")

    if extension == ".pdf":
        _validate_pdf(upload)
    elif extension == ".docx":
        _validate_docx(upload, file_size)
    else:
        _validate_txt(upload)

    result = ValidatedDocument(
        original_filename=original_filename,
        extension=extension,
        mime_type=mime_type,
        file_size=file_size,
        checksum_sha256=_checksum(upload),
    )
    setattr(upload, "_ai_validated_document", result)
    if underlying_upload is not upload:
        setattr(underlying_upload, "_ai_validated_document", result)
    return result
