"""
Main entry point for the Slack Community Agent.

HTTP webhook server for Slack Events API.

Startup sequence (see `init_app`):
  1. Validate configuration and DB connectivity
  2. Warm up the agent graph
  3. auth.test -> cache bot's own user ID
  4. users.list -> build slack_name -> user_id cache for the team

The module-level `app` object is the WSGI application. In Docker we run it behind
gunicorn (`gunicorn agent.main:app`); for local development `python -m agent.main`
still works and falls back to Flask's built-in server.
"""

import argparse
import atexit
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict

from flask import Flask, request, jsonify

from agent import dedup
from agent.config import Config, AGENT_NAME, COMPANY_NAME
from agent.slack_client import create_slack_client
from agent.graph import get_agent_graph
from agent.state import create_initial_state
from agent.logger import get_logger
from agent.persistence import get_database
from agent.team import (
    load_team,
    build_name_to_id_cache,
    set_bot_user_id,
    is_org_member_by_id,
)


app = Flask(__name__)
logger = get_logger(log_dir=Config.LOG_DIR, log_level=Config.LOG_LEVEL)
slack_client = create_slack_client()


# ---------------------------------------------------------------------------
# Bounded worker pool
# ---------------------------------------------------------------------------
# Slack expects a webhook response within 3s, so every event is processed in a
# background worker. A bounded pool caps concurrent processing and provides a
# graceful-shutdown hook for gunicorn SIGTERM.

_MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT_MESSAGES", "8")))
_executor = ThreadPoolExecutor(
    max_workers=_MAX_CONCURRENT,
    thread_name_prefix="slack-agent",
)


def _shutdown_executor() -> None:
    _executor.shutdown(wait=True, cancel_futures=False)


atexit.register(_shutdown_executor)


# ---------------------------------------------------------------------------
# One-time app initialization (idempotent, safe under gunicorn workers)
# ---------------------------------------------------------------------------

_initialized = False


def init_app() -> None:
    """
    Idempotent initialization: DB connectivity, graph warm-up, team cache.
    Safe to call from each gunicorn worker (runs exactly once per process).
    """
    global _initialized
    if _initialized:
        return

    db = get_database()
    try:
        db.check_connection()
    except RuntimeError as exc:
        logger.logger.error("Database connection failed during init: %s", exc)
        raise

    get_agent_graph()
    initialize_team()

    _initialized = True
    logger.logger.info("Agent initialized (pid=%d, workers=%d)", os.getpid(), _MAX_CONCURRENT)


def initialize_team() -> None:
    """
    Populate the team resolver:
      1. auth.test   -> bot's own user ID
      2. config/team.json
      3. users.list  -> slack_name -> user_id cache
    """
    try:
        bot_info = slack_client.client.auth_test()
        bot_user_id = bot_info.get("user_id") or bot_info.get("user")
        if bot_user_id:
            set_bot_user_id(bot_user_id)
        else:
            logger.logger.warning("Could not fetch bot user ID from auth.test")

        load_team()

        # Slack paginates users.list; fetch all pages.
        all_users = []
        cursor = None
        while True:
            kwargs = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = slack_client.client.users_list(**kwargs)
            members = resp.get("members", [])
            all_users.extend(members)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        build_name_to_id_cache(all_users)
        logger.logger.info("Team initialized successfully.")

    except Exception as e:
        logger.logger.error(
            f"Team initialization failed: {e}. Org-member features will use name-matching fallback."
        )


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def _slack_events_handler():
    """Handle Slack Events API webhook (shared by WEBHOOK_PATH and optional POST /)."""
    try:
        timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
        signature = request.headers.get('X-Slack-Signature', '')
        body = request.get_data(as_text=True)

        if not slack_client.verify_signature(timestamp, body, signature):
            logger.logger.warning("Invalid Slack signature")
            return jsonify({"error": "Invalid signature"}), 403

        data = request.get_json(silent=True) or {}

        # URL verification challenge — respond before any dedup/retry checks.
        if data.get('type') == 'url_verification':
            logger.logger.info("Handling URL verification challenge")
            return jsonify({"challenge": data.get('challenge')}), 200

        # Slack retries aggressively. If this request is a retry we already ack'd
        # the first delivery; silently acknowledge and drop to avoid duplicate replies.
        if dedup.is_retry(request.headers):
            retry_num = request.headers.get('X-Slack-Retry-Num', '')
            retry_reason = request.headers.get('X-Slack-Retry-Reason', '')
            logger.logger.info(
                "Ignoring Slack retry (num=%s reason=%s)", retry_num, retry_reason
            )
            return jsonify({"ok": True}), 200

        if data.get('type') == 'event_callback':
            event = data.get('event', {})
            event_type = event.get('type')

            if event_type == 'message':
                if slack_client.is_bot_message(event) or event.get('subtype'):
                    return jsonify({"ok": True}), 200

                # Skip messages from org members entirely (stand-alone only;
                # mid-thread detection has been removed).
                sender_id = event.get('user', '')
                if sender_id and is_org_member_by_id(sender_id):
                    logger.logger.info(
                        f"Skipping message from org member user_id={sender_id}"
                    )
                    return jsonify({"ok": True}), 200

                # Idempotency guard against duplicate deliveries that don't carry
                # the retry header (e.g. upstream replays).
                event_id = dedup.event_id_from(event)
                if dedup.seen(event_id):
                    logger.logger.info("Skipping duplicate event id=%s", event_id)
                    return jsonify({"ok": True}), 200

                _executor.submit(_process_message_safe, data)

        return jsonify({"ok": True}), 200

    except Exception as e:
        logger.log_error(
            error_type="WebhookError",
            error_message=str(e),
            stack_trace=str(e),
        )
        return jsonify({"error": str(e)}), 500


app.add_url_rule(
    Config.WEBHOOK_PATH,
    endpoint="slack_events",
    view_func=_slack_events_handler,
    methods=["POST"],
)
# Slack "Request URL" is often pasted as the ngrok base URL without /slack/events — accept POST / too.
if Config.WEBHOOK_PATH != "/":
    app.add_url_rule(
        "/",
        endpoint="slack_events_root",
        view_func=_slack_events_handler,
        methods=["POST"],
    )


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def _process_message_safe(event_data: Dict[str, Any]) -> None:
    """Executor entry point: never let an exception escape the worker."""
    try:
        process_message(event_data)
    except Exception as e:
        logger.log_error(
            error_type="MessageProcessingError",
            error_message=str(e),
            stack_trace=str(e),
        )


def process_message(event_data: Dict[str, Any]) -> None:
    """Process a Slack message event through the agent graph."""
    event = event_data.get('event', {})

    logger.log_message_received(
        user_id=event.get('user', ''),
        channel_id=event.get('channel', ''),
        text=event.get('text', ''),
        thread_ts=event.get('thread_ts'),
        user_profile=None,
    )

    slack_client.add_reaction(
        channel=event.get('channel'),
        timestamp=event.get('ts'),
        emoji='eyes',
    )

    initial_state = create_initial_state(event_data)
    graph = get_agent_graph()
    graph.invoke(initial_state)


# ---------------------------------------------------------------------------
# Utility endpoints
# ---------------------------------------------------------------------------

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "agent": f"{COMPANY_NAME} Slack Community Agent",
        "agent_name": AGENT_NAME,
        "version": "1.0.0",
    }), 200


@app.route('/stats', methods=['GET'])
def get_stats():
    """Get agent statistics."""
    try:
        db = get_database()
        stats = db.get_stats()
        return jsonify(stats), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def main():
    """CLI entry point for local development (`uv run slack-agent`)."""
    parser = argparse.ArgumentParser(description="Slack Community Agent")
    parser.add_argument('--port', type=int, default=Config.WEBHOOK_PORT)
    parser.add_argument('--validate-config', action='store_true')
    parser.add_argument('--stats', action='store_true')
    args = parser.parse_args()

    if args.validate_config:
        Config.print_config()
        return 0 if Config.validate() else 1

    if args.stats:
        if not Config.validate():
            return 1
        db = get_database()
        stats = db.get_stats()
        print("\nSlack Agent Statistics")
        print("=" * 50)
        for k, v in stats.items():
            print(f"{k}: {v}")
        print("=" * 50 + "\n")
        return 0

    if not Config.validate():
        print("\nConfiguration validation failed. Please check your .env file.")
        return 1

    Config.print_config()

    try:
        init_app()
    except RuntimeError as exc:
        print(f"\nDatabase connection failed:\n   {exc}\n")
        return 1

    logger.logger.info("=" * 60)
    logger.logger.info("%s Slack Community Agent Starting (%s)", COMPANY_NAME, AGENT_NAME)
    logger.logger.info("=" * 60)
    logger.logger.info(f"Webhook: http://localhost:{args.port}{Config.WEBHOOK_PATH}")
    logger.logger.info(f"Health:  http://localhost:{args.port}/health")
    logger.logger.info(f"Stats:   http://localhost:{args.port}/stats")
    logger.logger.info("=" * 60)

    # Flask's dev server is fine locally; production uses gunicorn (see Dockerfile).
    app.run(host='0.0.0.0', port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
else:
    # Imported by a WSGI runner (e.g. gunicorn `agent.main:app`): initialize
    # eagerly so the first request doesn't race against DB / graph warm-up.
    # Swallow failures so the worker still boots and /health remains reachable;
    # per-request handlers will surface the real error.
    if os.getenv("SLACK_AGENT_SKIP_AUTOINIT", "").strip().lower() not in ("1", "true", "yes"):
        try:
            init_app()
        except Exception as exc:  # noqa: BLE001
            logger.logger.error("init_app() failed at import: %s", exc)
