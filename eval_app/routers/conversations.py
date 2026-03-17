"""
API routes for listing and fetching conversations.
"""

from fastapi import APIRouter, HTTPException, Query

from eval_app import db

router = APIRouter(prefix="/api", tags=["conversations"])


@router.get("/conversations")
def list_conversations(
    sort: str = Query("created_at_desc", description="created_at_desc | created_at_asc"),
    status: str = Query(
        "all",
        description="all | resolved | escalated | needs_clarification | no_response",
    ),
    source: str = Query(
        "all",
        description="all | slack | local_test",
    ),
    q: str | None = Query(None, description="Search in message_text and response_text"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List conversations with optional filters and sort."""
    try:
        items, total = db.list_conversations(
            sort=sort,
            status=status,
            source=source,
            q=q,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total}
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/conversations/{conversation_id:int}")
def get_conversation(conversation_id: int):
    """Get a single conversation by id."""
    try:
        row = db.get_conversation(conversation_id)
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return row
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
