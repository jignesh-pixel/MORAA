"""Image upload API routes."""

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.image import UploadResponse
from app.services.upload_service import UploadService
from app.utils.logger import logger
from app.utils.upload_limits import read_capped

router = APIRouter(prefix="/api", tags=["Upload"])


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a jewellery image",
    description=(
        "Upload a jewellery image for analysis. "
        "Supports JPEG, PNG, and WEBP formats. "
        "Maximum file size is configurable (default 10MB)."
    ),
)
async def upload_image(
    file: UploadFile = File(..., description="Image file to upload"),
    db: Session = Depends(get_db),
):
    """
    Upload a jewellery image file.

    The file is validated for type and size, then saved to the
    configured upload directory. Returns image metadata including
    a unique ID and URL for subsequent analysis.
    """
    service = UploadService(db)

    # Read file data
    try:
        file_data = await read_capped(file)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    file_size = len(file_data)
    filename = file.filename or "untitled"
    mime_type = file.content_type or "image/jpeg"

    try:
        result = await service.process_upload(
            file_data=file_data,
            filename=filename,
            file_size=file_size,
            mime_type=mime_type,
        )
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Upload failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process upload",
        )
