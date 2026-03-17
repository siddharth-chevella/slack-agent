"""
Agent Evaluation Dashboard — FastAPI app.

Serves a minimalistic UI to evaluate the Slack agent: view user questions
and agent responses with filters (status, source: Slack vs local test) and sort by date.
"""

from pathlib import Path

from fastapi import Request
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from eval_app import db
from eval_app.routers import conversations, errors, stats

app = FastAPI(title="Agent Evaluation Dashboard", version="1.0.0")

# Routers
app.include_router(conversations.router)
app.include_router(stats.router)
app.include_router(errors.router)

# Templates and static (relative to eval_app package)
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Avoid 404 for favicon requests."""
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    source: str = "all",
    status: str = "all",
    sort: str = "created_at_desc",
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Serve the evaluation dashboard with server-rendered list."""
    try:
        items, total = db.list_conversations(
            sort=sort,
            status=status,
            source=source,
            q=q,
            limit=limit,
            offset=offset,
        )
        # Attach node lineage (each node's full output JSON) for dropdown
        if items:
            import json as _json
            by_msg = db.get_node_outputs_bulk([i["message_ts"] for i in items])
            for item in items:
                node_outputs = by_msg.get(item["message_ts"], [])
                for no in node_outputs:
                    no["output_json_str"] = _json.dumps(no["output_json"], indent=2)
                item["node_outputs"] = node_outputs
    except FileNotFoundError:
        items = []
        total = 0
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "items": items,
            "total": total,
            "source": source,
            "status": status,
            "sort": sort,
            "q": q or "",
            "limit": limit,
            "offset": offset,
        },
    )


@app.get("/errors", response_class=HTMLResponse)
async def errors_page(request: Request, limit: int = 100):
    """Serve the errors log page."""
    import json
    from pathlib import Path
    from eval_app.config import LOG_DIR
    log_path = Path(LOG_DIR) / "errors.jsonl"
    items = []
    if log_path.exists():
        try:
            with open(log_path) as f:
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
        except OSError:
            pass
    return templates.TemplateResponse(
        "errors.html",
        {"request": request, "items": items, "total": len(items)},
    )


@app.get("/health")
def health():
    return {"status": "healthy", "app": "eval_dashboard"}
