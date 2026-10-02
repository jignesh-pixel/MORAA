"""History API routes."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.common import DeleteResponse
from app.schemas.history import (
    HistoryItemResponse,
    HistoryListResponse,
    HistoryStatsResponse,
)
from app.services.history_service import HistoryService

router = APIRouter(prefix="/api/history", tags=["History"])


@router.get(
    "",
    response_model=HistoryListResponse,
    summary="Get analysis history",
    description="Get paginated list of all previous jewellery analyses.",
)
def get_history(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    status: Optional[str] = Query(None, description="Filter by status"),
    search: Optional[str] = Query(None, description="Search in product names"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get paginated analysis history."""
    service = HistoryService(db)
    user_id = current_user.id if current_user else None
    return service.get_history(
        user_id=user_id,
        page=page,
        page_size=page_size,
        status=status,
        search=search,
    )


@router.get(
    "/stats",
    response_model=HistoryStatsResponse,
    summary="Get history statistics",
    description="Get aggregate statistics about analysis history.",
)
def get_history_stats(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get history statistics (total count, monthly count, average price)."""
    service = HistoryService(db)
    user_id = current_user.id if current_user else None
    return service.get_stats(user_id=user_id)


@router.get(
    "/{history_id}",
    response_model=HistoryItemResponse,
    summary="Get history item",
    description="Get a specific history entry by ID.",
)
def get_history_item(
    history_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get a single history entry."""
    service = HistoryService(db)
    user_id = current_user.id if current_user else None
    item = service.get_history_item(history_id, user_id=user_id)

    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="History entry not found",
        )

    return item


@router.delete(
    "/{history_id}",
    response_model=DeleteResponse,
    summary="Delete history item",
    description="Delete a specific history entry by ID.",
)
def delete_history_item(
    history_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Delete a history entry."""
    service = HistoryService(db)
    user_id = current_user.id if current_user else None
    deleted = service.delete_history_item(history_id, user_id=user_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="History entry not found",
        )

    return DeleteResponse(success=True, message="History entry deleted")
