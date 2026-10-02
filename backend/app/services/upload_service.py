"""Upload service for handling image file uploads.

Every uploaded image receives a globally unique ``request_id`` (UUID)
and is stored inside its own isolated sub-directory::

    uploads/
        <request_id>/
            original.<ext>

This guarantees zero file collisions, even under heavy concurrency with
multiple users or browser tabs.

Extended with SHA-256 image hashing, processing lifecycle logging, and
session tracking for enterprise-grade request traceability.
"""

import hashlib
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.image import Image
from app.repositories.base import BaseRepository
from app.schemas.image import UploadResponse
from app.services.processing_service import ProcessingService
from app.utils.file_helpers import (
    delete_directory,
    delete_file,
    get_file_extension,
    get_image_dimensions,
    get_mime_type,
    generate_storage_path,
    save_upload_file,
    sanitize_filename,
    validate_file_extension,
    validate_file_size,
)
from app.utils.executors import run_cpu, run_io
from app.utils.logger import logger


class UploadService:
    """Image upload management service."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = BaseRepository(Image, db)

    def validate_upload(self, filename: str, file_size: int) -> None:
        """Validate uploaded file."""
        if not filename or "." not in filename:
            raise ValueError("Invalid filename")

        if not validate_file_extension(filename):
            allowed = ", ".join(settings.ALLOWED_EXTENSIONS_LIST)
            raise ValueError(
                f"Invalid file type. Allowed: {allowed}"
            )

        if not validate_file_size(file_size):
            max_mb = settings.MAX_UPLOAD_SIZE_MB
            raise ValueError(
                f"File too large. Maximum size is {max_mb}MB"
            )

    async def process_upload(
        self,
        file_data: bytes,
        filename: str,
        file_size: int,
        mime_type: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> UploadResponse:
        """
        Process and save an uploaded image.

        Generates a globally unique ``request_id``, stores the file in
        ``uploads/<request_id>/original.<ext>``, records the metadata in
        the database, computes the SHA-256 hash, and logs every step.

        The ``request_id`` is the single correlation ID used throughout
        the entire processing lifecycle.
        """
        # ── Generate request ID ──────────────────────────────────────────
        request_id = str(uuid.uuid4())

        # ── Validate ─────────────────────────────────────────────────────
        self.validate_upload(filename, file_size)

        # ── Sanitise filename ────────────────────────────────────────────
        safe_filename = sanitize_filename(filename)

        # Hashing, writing the file and reading its dimensions are CPU / disk work (PERF-6): done on the CPU
        # pool. The database records are written on the I/O pool (PERF-2), so a slow remote database round
        # trip never blocks the event loop. The session is used by one thread at a time (awaited in turn).
        image_hash, file_path, stored_name, width, height = await run_cpu(
            self._store_file, file_data, request_id, safe_filename
        )
        return await run_io(
            self._persist_upload,
            request_id, safe_filename, filename, file_size, mime_type,
            image_hash, file_path, stored_name, width, height, user_id, session_id,
        )

    def _store_file(self, file_data: bytes, request_id: str, safe_filename: str):
        """Hash the bytes, store them in the isolated request directory, read the image size (blocking)."""
        # ── Compute SHA-256 hash for integrity & deduplication ───────────
        image_hash = hashlib.sha256(file_data).hexdigest()

        # ── Store file in isolated directory ─────────────────────────────
        upload_dir = settings.UPLOAD_PATH
        file_path, stored_name = generate_storage_path(upload_dir, request_id, safe_filename)
        save_upload_file(file_path.parent, file_data, file_path.name)

        # ── Extract metadata ─────────────────────────────────────────────
        width, height = get_image_dimensions(str(file_path))
        return image_hash, file_path, stored_name, width, height

    def _persist_upload(
        self,
        request_id: str,
        safe_filename: str,
        filename: str,
        file_size: int,
        mime_type: str,
        image_hash: str,
        file_path,
        stored_name: str,
        width,
        height,
        user_id: Optional[str],
        session_id: Optional[str],
    ) -> UploadResponse:
        """Write the image record, the processing steps and the audit row (blocking database work)."""
        proc = ProcessingService(self.db)
        ext = get_file_extension(safe_filename)
        image_url = f"/uploads/{request_id}/{file_path.name}"

        # ── Persist database record FIRST (FK must exist before logs) ────
        image = self.repo.create(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            original_filename=safe_filename,
            stored_filename=stored_name,
            file_path=str(file_path),
            file_size=file_size,
            mime_type=mime_type or get_mime_type(ext),
            image_hash=image_hash,
            image_url=image_url,
            processing_status="stored",
            width=width,
            height=height,
        )

        # ── Log processing steps (Image FK must exist) ───────────────────
        proc.log_processing_step(
            request_id, "upload", "started",
            metadata={"filename": filename, "file_size": file_size, "mime_type": mime_type},
        )
        proc.log_processing_step(request_id, "validation", "completed")
        proc.log_processing_step(
            request_id, "storage", "completed",
            metadata={
                "stored_path": str(file_path),
                "stored_name": stored_name,
                "image_hash": image_hash,
            },
        )

        logger.bind(category="upload").info(
            f"Upload complete: request_id={request_id} "
            f"filename={safe_filename} -> {file_path} "
            f"({file_size} bytes, {mime_type}) "
            f"sha256={image_hash[:16]}..."
        )

        # ── Log: upload completed ────────────────────────────────────────
        proc.log_processing_step(request_id, "upload", "completed")

        # ── Log audit event ──────────────────────────────────────────────
        proc.log_audit(
            action="upload",
            status="success",
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            resource_type="image",
            resource_id=image.id,
            details=f"Uploaded {safe_filename} ({file_size} bytes)",
        )

        return UploadResponse(
            id=image.id,
            request_id=request_id,
            filename=safe_filename,
            url=image.image_url,
            size=image.file_size,
            mime_type=image.mime_type,
            uploaded_at=image.created_at,
        )

    def get_image(self, image_id: str) -> Optional[Image]:
        """Get image by ID."""
        return self.repo.get(image_id)

    def delete_image(self, image_id: str) -> bool:
        """Delete an image, its file, and its isolated request directory."""
        image = self.repo.get(image_id)
        if not image:
            return False

        # Delete file from disk
        delete_file(image.file_path)

        # Clean up the isolated request directory if it exists
        import os
        request_dir = os.path.dirname(image.file_path)
        if request_dir:
            delete_directory(request_dir)

        # Delete from database
        self.repo.delete(image_id)

        logger.bind(category="upload").info(
            f"Image deleted: {image.stored_filename} "
            f"(request_id={image.request_id})"
        )
        return True
