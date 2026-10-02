"""Batch upload API route — upload multiple images for one analysis.

All uploaded images share a single ``group_id`` and can be analysed
together via the group analysis endpoint.
"""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.image import Image
from app.schemas.analysis_group import (
    BatchUploadImage,
    BatchUploadResponse,
)
from app.services.processing_service import ProcessingService
from app.services.upload_service import UploadService
from app.utils.executors import run_io
from app.utils.logger import logger
from app.utils.upload_limits import read_capped

router = APIRouter(prefix="/api", tags=["Batch Upload"])


def _log_batch_steps(db: Session, proc: ProcessingService, group_id: str, image_ids: List[str], errors: List[str]) -> None:
    """Write one "batch_upload" processing step per stored image (blocking; runs on the I/O pool).

    Steps are keyed by an image's request_id (a foreign key to images), looked up here in one query. The group_id
    and position go in the metadata. A logging problem never fails the upload itself."""
    try:
        rows = db.query(Image.id, Image.request_id).filter(Image.id.in_(image_ids)).all()
        request_by_image = {image_id: request_id for image_id, request_id in rows}
    except Exception as lookup_error:  # noqa: BLE001
        db.rollback()
        logger.warning(f"Batch steps not logged (lookup failed): {lookup_error}")
        return
    for position, image_id in enumerate(image_ids):
        request_id = request_by_image.get(image_id)
        if not request_id:
            continue
        try:
            proc.log_processing_step(
                request_id, "batch_upload", "completed",
                metadata={
                    "group_id": group_id,
                    "order": position,
                    "uploaded": len(image_ids),
                    "failed": len(errors),
                    "errors": errors if errors else None,
                },
            )
        except Exception as log_error:  # noqa: BLE001
            db.rollback()
            logger.warning(f"Batch step not logged for {request_id}: {log_error}")


@router.post(
    "/upload/batch",
    response_model=BatchUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload multiple images for a single analysis",
    description=(
        "Upload multiple images at once. All images share a group_id "
        "so they can be analysed as a single analysis group."
    ),
)
async def upload_images_batch(
    files: List[UploadFile],
    db: Session = Depends(get_db),
):
    """Upload multiple images and assign them to a single analysis group.

    Each image is validated, stored, and linked via ``group_id``.
    All images must be valid JPEG, PNG, or WEBP files under the
    configured size limit.
    """
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided. Please select at least one image.",
        )

    if len(files) > 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Maximum 20 images per batch upload.",
        )

    service = UploadService(db)
    proc = ProcessingService(db)
    group_id = str(uuid.uuid4())
    uploaded_images: List[BatchUploadImage] = []
    errors: List[str] = []

    # NOTE: processing steps are keyed by an IMAGE's request_id (a foreign key to images). The batch's group_id
    # is not an image, so logging against it failed with a foreign-key error on every batch (DATA-8). The batch
    # is logged below against the real image request ids instead.

    for order, file in enumerate(files):
        try:
            file_data = await read_capped(file)
            file_size = len(file_data)
            filename = file.filename or f"untitled_{order}"
            mime_type = file.content_type or "image/jpeg"

            result = await service.process_upload(
                file_data=file_data,
                filename=filename,
                file_size=file_size,
                mime_type=mime_type,
            )

            uploaded_images.append(
                BatchUploadImage(
                    id=result.id,
                    filename=result.filename,
                    url=result.url,
                    size=result.size,
                    order=order,
                )
            )
        except ValueError as e:
            errors.append(f"{file.filename}: {str(e)}")
        except Exception as e:
            logger.error(f"Batch upload failed for {file.filename}: {e}")
            errors.append(f"{file.filename}: Failed to process upload")

    # One batch step per stored image (its request_id exists in images, so the foreign key holds); the group_id
    # and position are in the metadata so the whole batch can still be found. A logging problem never fails the
    # upload itself.
    if uploaded_images:
        await run_io(_log_batch_steps, db, proc, group_id, [i.id for i in uploaded_images], errors)

    if not uploaded_images:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No images were uploaded successfully. Errors: {'; '.join(errors)}",
        )

    response = BatchUploadResponse(
        group_id=group_id,
        images=uploaded_images,
        count=len(uploaded_images),
    )

    # Store group_id reference in processing logs for traceability
    for img in uploaded_images:
        logger.bind(category="upload").info(
            f"Batch upload: group_id={group_id} "
            f"image_id={img.id} filename={img.filename} order={img.order}"
        )

    logger.info(
        f"Batch upload complete: {len(uploaded_images)} images, "
        f"{len(errors)} errors, group_id={group_id}"
    )

    return response
