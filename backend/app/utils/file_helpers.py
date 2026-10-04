"""File handling utilities for upload validation and storage."""

import os
import re
import uuid
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image as PILImage

from app.config import settings


# ── Validation ─────────────────────────────────────────────────────────────


def validate_file_extension(filename: str) -> bool:
    """Check if file extension is allowed."""
    ext = get_file_extension(filename)
    return ext in settings.ALLOWED_EXTENSIONS_LIST


def validate_file_size(file_size: int) -> bool:
    """Check if file size is within limits."""
    return file_size <= settings.MAX_UPLOAD_SIZE_BYTES


def sanitize_filename(filename: str) -> str:
    """
    Strip path separators and dangerous characters from a filename.

    - Removes any leading directory components (``../../etc`` → ``etc``)
    - Retains only basename (last path component)
    - Strips null bytes and control characters
    - Returns a safe, flat filename string.
    """
    # Remove null bytes and path separators
    cleaned = filename.replace("\x00", "").replace("\\", "/")
    # Keep only the basename (last component after any `/`)
    basename = cleaned.rstrip("/").split("/")[-1] if "/" in cleaned else cleaned
    # Remove any remaining dangerous characters (keep letters, digits, . - _)
    safe = re.sub(r"[^\w.\-]", "_", basename)
    # Trim leading/trailing whitespace and dots
    safe = safe.strip(". ")
    return safe or "untitled"


def get_file_extension(filename: str) -> str:
    """Extract file extension from filename."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


# ── Storage helpers ────────────────────────────────────────────────────────


def generate_storage_path(
    upload_dir: Path, request_id: str, original_filename: str
) -> Tuple[Path, str]:
    """
    Create an isolated per-request subdirectory and return the full path
    together with the safe stored filename.

    Directory layout::

        uploads/
            <request_id>/
                original.<ext>

    This guarantees that every uploaded image lives in its own directory,
    so no two uploads can ever overwrite each other.
    """
    ext = get_file_extension(original_filename)
    safe_name = sanitize_filename(original_filename)

    # Build the subdirectory path
    sub_dir = upload_dir / request_id
    sub_dir.mkdir(parents=True, exist_ok=True)

    # Use a deterministic but unique stored name inside the isolated dir
    stored_name = f"original.{ext}" if ext else f"original.{safe_name.rsplit('.', 1)[-1] if '.' in safe_name else 'bin'}"
    file_path = sub_dir / stored_name

    return file_path, stored_name


def save_upload_file(
    upload_dir: Path, file_data: bytes, stored_filename: str
) -> str:
    """
    Save uploaded file to disk.

    Args:
        upload_dir: Destination directory.
        file_data: Raw file bytes.
        stored_filename: Name of the file to write (e.g. ``original.jpg``).

    Returns:
        Absolute file path as a string.
    """
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / stored_filename
    with open(file_path, "wb") as f:
        f.write(file_data)
    return str(file_path)


# ── Image introspection ────────────────────────────────────────────────────


def get_image_dimensions(file_path: str) -> Tuple[Optional[int], Optional[int]]:
    """Get image width and height using Pillow."""
    try:
        with PILImage.open(file_path) as img:
            return img.width, img.height
    except Exception:
        return None, None


def get_mime_type(extension: str) -> str:
    """Get MIME type from file extension."""
    mime_map = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }
    return mime_map.get(extension, "application/octet-stream")


def delete_file(file_path: str) -> bool:
    """Delete a file from disk. Returns True if deleted."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            return True
        return False
    except Exception:
        return False


def delete_directory(dir_path: str) -> bool:
    """
    Recursively delete an empty or non-empty directory.

    Used to clean up per-request upload directories when an image is deleted.
    """
    try:
        if os.path.exists(dir_path):
            import shutil
            shutil.rmtree(dir_path, ignore_errors=True)
            return True
        return False
    except Exception:
        return False


# Folders inside uploads/ that do not belong to one upload request and must survive the orphan clean-up.
PROTECTED_DIRECTORIES = frozenset({"outputs"})


def cleanup_orphaned_directories(
    upload_dir: Path, known_request_ids: set
) -> int:
    """
    Remove upload sub-directories that have no matching Image record.

    Deletes ONLY directories that are not referenced by any record in the
    database — i.e. leftovers from an aborted upload or a crash between
    file-save and DB-commit. Every directory that has a DB record is
    permanent user data and is kept untouched.

    Args:
        upload_dir: Root ``uploads/`` directory.
        known_request_ids: Set of ``request_id`` values currently referenced
            by the Image table.

    Returns:
        Number of orphaned directories removed.
    """
    if not upload_dir.exists() or not upload_dir.is_dir():
        return 0
    removed = 0
    for entry in upload_dir.iterdir():
        if entry.is_dir() and entry.name in PROTECTED_DIRECTORIES:
            continue                                  # images we produced (chat dashboard record): never an orphan
        if entry.is_dir() and entry.name not in known_request_ids:
            if delete_directory(str(entry)):
                removed += 1
    return removed
