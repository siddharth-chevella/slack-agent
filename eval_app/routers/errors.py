"""
API routes for agent error log (optional).
"""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from eval_app.config import LOG_DIR

router = APIRouter(prefix="/api", tags=["errors"])

ERRORS_LOG = Path(LOG_DIR) / "errors.jsonl"


@router.get("/errors")
def get_errors(
    limit: int = Query(100, ge=1, le=500),
):
    """Return last N error entries from logs/errors.jsonl."""
    if not ERRORS_LOG.exists():
        return {"items": [], "total": 0}
    items = []
    try:
        with open(ERRORS_LOG) as f:
            lines = f.readlines()
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        items.reverse()
        return {"items": items, "total": len(items)}
    except OSError as e:
        raise HTTPException(status_code=503, detail=str(e))
