"""
API routes for aggregate stats.
"""

from fastapi import APIRouter, HTTPException, Query

from eval_app import db

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats")
def get_stats(
    source: str | None = Query(None, description="all | slack | local_test"),
):
    """Get aggregate statistics. Optionally filter by source."""
    try:
        stats = db.get_stats(source=source)
        return stats
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
