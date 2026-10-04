"""Report API routes for generating and downloading reports."""

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.schemas.report import (
    ReportConfigRequest,
    ReportGenerateRequest,
    ReportResponse,
)
from app.services.report_service import ReportService

router = APIRouter(prefix="/api/reports", tags=["Reports"])


@router.post(
    "/generate",
    response_model=ReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a report",
    description="Generate a PDF report from analysis data.",
)
async def generate_report(
    request: ReportConfigRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Generate a new report based on the provided configuration."""
    service = ReportService(db)
    try:
        user_id = current_user.id if current_user else None
        result = await service.generate_report(
            config=request,
            user_id=user_id,
        )
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get(
    "/download/{report_id}",
    summary="Download a report",
    description="Download a generated report file.",
)
def download_report(
    report_id: str,
    db: Session = Depends(get_db),
):
    """Download a generated report file."""
    service = ReportService(db)
    file_path = service.get_report_file_path(report_id)

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report file not found",
        )

    report = service.get_report(report_id)
    filename = report.filename if report else "report.pdf"

    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="application/pdf",
    )
