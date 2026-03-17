"""
Run the Agent Evaluation Dashboard.

Usage:
    uv run eval-dashboard
    # or
    uvicorn eval_app.main:app --reload --port 8000
"""

import uvicorn

from eval_app.config import EVAL_APP_PORT


def main():
    uvicorn.run(
        "eval_app.main:app",
        host="0.0.0.0",
        port=EVAL_APP_PORT,
        reload=True,
    )


if __name__ == "__main__":
    main()
