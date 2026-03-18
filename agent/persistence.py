"""
Database persistence for OLake Slack Community Agent.

Manages storage of conversations, user profiles, interaction history,
and per-node output lineage for each conversation.
"""

import dataclasses
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from contextlib import contextmanager
from enum import Enum

from agent.state import (
    ConversationRecord,
    UserInteraction,
    UserProfile,
    IntentType,
)
from agent.logger import get_logger


def _sanitize_for_json(obj: Any) -> Any:
    """Convert state (or any nested structure) to JSON-serializable form. Skips keys starting with _."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _sanitize_for_json(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(x) for x in obj]
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


class Database:
    """SQLite database for agent persistence."""
    
    def __init__(self, db_path: str = "data/slack_agent.db"):
        """
        Initialize database connection.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger()
        self._init_database()
    
    @contextmanager
    def get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def _init_database(self) -> None:
        """Initialize database schema."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Conversations table (urgency removed as deprecated)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_ts TEXT NOT NULL UNIQUE,
                    thread_ts TEXT,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_query TEXT NOT NULL,
                    intent_type TEXT,
                    response_text TEXT,
                    confidence REAL DEFAULT 0.0,
                    needs_clarification INTEGER DEFAULT 0,
                    escalated INTEGER DEFAULT 0,
                    escalation_reason TEXT,
                    docs_cited TEXT,
                    reasoning_summary TEXT,
                    processing_time REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved INTEGER DEFAULT 0,
                    resolved_at TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_user 
                ON conversations(user_id, created_at DESC)
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_thread 
                ON conversations(thread_ts)
            """)
            
            # User interactions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    message_ts TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    topic TEXT,
                    resolved INTEGER DEFAULT 0,
                    resolution_time REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (message_ts) REFERENCES conversations(message_ts)
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_interactions 
                ON user_interactions(user_id, created_at DESC)
            """)
            
            # User profiles table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    real_name TEXT,
                    email TEXT,
                    total_messages INTEGER DEFAULT 0,
                    common_topics TEXT,
                    resolved_issues INTEGER DEFAULT 0,
                    unresolved_issues INTEGER DEFAULT 0,
                    avg_resolution_time REAL DEFAULT 0.0,
                    last_interaction TIMESTAMP,
                    knowledge_level TEXT DEFAULT 'beginner',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Documentation lookups cache
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS documentation_lookups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    results TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_doc_lookups_query 
                ON documentation_lookups(query, created_at DESC)
            """)
            
            # Migration: add retrieval columns to conversations if missing
            try:
                cursor.execute("PRAGMA table_info(conversations)")
                cols = [row[1] for row in cursor.fetchall()]
                if "retrieval_queries" not in cols:
                    cursor.execute("ALTER TABLE conversations ADD COLUMN retrieval_queries TEXT")
                if "retrieval_file_paths" not in cols:
                    cursor.execute("ALTER TABLE conversations ADD COLUMN retrieval_file_paths TEXT")
            except Exception:
                pass

            # Migration: drop deprecated urgency column (SQLite 3.35+)
            try:
                cursor.execute("PRAGMA table_info(conversations)")
                cols = [row[1] for row in cursor.fetchall()]
                if "urgency" in cols:
                    cursor.execute("ALTER TABLE conversations DROP COLUMN urgency")
            except Exception:
                pass

            # Node outputs: full lineage per conversation (each node's full state output as JSON)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS node_outputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_ts TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    step_order INTEGER NOT NULL,
                    output_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_node_outputs_message_ts
                ON node_outputs(message_ts, step_order)
            """)
            
            # DEBUG only: avoid cluttering console; this is SQLite persistence, not vectordb
            self.logger.logger.debug("Persistence database (SQLite) ready")
    
    def save_documentation_lookup(
        self,
        query: str,
        results: str,
    ) -> int:
        """
        Persist a single retrieval batch (e.g. one round of queries and their results).
        
        Args:
            query: JSON array of queries run, or single query string
            results: JSON array of result items (e.g. [{"path", "source"}])
            
        Returns:
            ID of inserted row
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO documentation_lookups (query, results)
                VALUES (?, ?, ?)
            """, (query, results))
            return cursor.lastrowid
    
    def save_conversation(self, record: ConversationRecord) -> int:
        """
        Save a conversation record.
        
        Args:
            record: ConversationRecord to save
            
        Returns:
            ID of inserted record
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(conversations)")
            cols = [row[1] for row in cursor.fetchall()]
            has_urgency = "urgency" in cols
            if has_urgency:
                cursor.execute("""
                    INSERT INTO conversations (
                        message_ts, thread_ts, channel_id, user_id, user_query,
                        intent_type, urgency, response_text, confidence,
                        needs_clarification, escalated, escalation_reason,
                        docs_cited, reasoning_summary, processing_time,
                        created_at, resolved, resolved_at,
                        retrieval_queries, retrieval_file_paths
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.message_ts,
                    record.thread_ts,
                    record.channel_id,
                    record.user_id,
                    record.user_query,
                    record.intent_type,
                    None,  # deprecated urgency
                    record.response_text,
                    record.confidence,
                    1 if record.needs_clarification else 0,
                    1 if record.escalated else 0,
                    record.escalation_reason,
                    record.docs_cited,
                    record.reasoning_summary,
                    record.processing_time,
                    record.created_at,
                    1 if record.resolved else 0,
                    record.resolved_at,
                    record.retrieval_queries,
                    record.retrieval_file_paths,
                ))
            else:
                cursor.execute("""
                    INSERT INTO conversations (
                        message_ts, thread_ts, channel_id, user_id, user_query,
                        intent_type, response_text, confidence,
                        needs_clarification, escalated, escalation_reason,
                        docs_cited, reasoning_summary, processing_time,
                        created_at, resolved, resolved_at,
                        retrieval_queries, retrieval_file_paths
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.message_ts,
                    record.thread_ts,
                    record.channel_id,
                    record.user_id,
                    record.user_query,
                    record.intent_type,
                    record.response_text,
                    record.confidence,
                    1 if record.needs_clarification else 0,
                    1 if record.escalated else 0,
                    record.escalation_reason,
                    record.docs_cited,
                    record.reasoning_summary,
                    record.processing_time,
                    record.created_at,
                    1 if record.resolved else 0,
                    record.resolved_at,
                    record.retrieval_queries,
                    record.retrieval_file_paths,
                ))
            
            return cursor.lastrowid

    def save_node_output(
        self,
        message_ts: str,
        node_name: str,
        step_order: int,
        output: Dict[str, Any],
    ) -> int:
        """
        Persist a single node's full output (state after that node) as JSON for lineage.
        """
        payload = _sanitize_for_json(output)
        output_json = json.dumps(payload)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO node_outputs (message_ts, node_name, step_order, output_json)
                VALUES (?, ?, ?, ?)
            """, (message_ts, node_name, step_order, output_json))
            return cursor.lastrowid

    def get_node_outputs(self, message_ts: str) -> List[Dict[str, Any]]:
        """Return node outputs for a conversation (message_ts), ordered by step_order."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT node_name, step_order, output_json, created_at
                FROM node_outputs
                WHERE message_ts = ?
                ORDER BY step_order ASC
            """, (message_ts,))
            rows = cursor.fetchall()
        out = []
        for row in rows:
            out.append({
                "node_name": row["node_name"],
                "step_order": row["step_order"],
                "output_json": json.loads(row["output_json"]) if row["output_json"] else {},
                "created_at": row["created_at"],
            })
        return out
    
    def get_user_recent_messages(
        self,
        user_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get user's recent messages."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM conversations
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (user_id, limit))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_thread_messages(
        self,
        thread_ts: str
    ) -> List[Dict[str, Any]]:
        """Get all messages in a thread."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM conversations
                WHERE thread_ts = ? OR message_ts = ?
                ORDER BY created_at ASC
            """, (thread_ts, thread_ts))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def update_user_profile(
        self,
        user_id: str,
        username: str,
        real_name: str,
        email: Optional[str] = None
    ) -> None:
        """Create or update user profile."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Calculate stats
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_messages,
                    SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) as resolved,
                    SUM(CASE WHEN resolved = 0 AND needs_clarification = 0 AND escalated = 0 THEN 1 ELSE 0 END) as unresolved,
                    AVG(CASE WHEN resolved = 1 THEN processing_time ELSE NULL END) as avg_time
                FROM conversations
                WHERE user_id = ?
            """, (user_id,))
            
            stats = dict(cursor.fetchone())
            
            # Get common topics (extract from message text - simplified)
            cursor.execute("""
                SELECT user_query FROM conversations
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 20
            """, (user_id,))
            
            messages = [row["user_query"] for row in cursor.fetchall()]
            # Simple topic extraction (in real implementation, use NLP)
            common_topics = []  # Placeholder
            
            # Determine knowledge level based on interactions
            knowledge_level = "beginner"
            if stats["total_messages"] > 20:
                knowledge_level = "advanced"
            elif stats["total_messages"] > 5:
                knowledge_level = "intermediate"
            
            cursor.execute("""
                INSERT INTO user_profiles (
                    user_id, username, real_name, email, total_messages,
                    common_topics, resolved_issues, unresolved_issues,
                    avg_resolution_time, last_interaction, knowledge_level, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    real_name = excluded.real_name,
                    email = excluded.email,
                    total_messages = excluded.total_messages,
                    common_topics = excluded.common_topics,
                    resolved_issues = excluded.resolved_issues,
                    unresolved_issues = excluded.unresolved_issues,
                    avg_resolution_time = excluded.avg_resolution_time,
                    last_interaction = CURRENT_TIMESTAMP,
                    knowledge_level = excluded.knowledge_level,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                user_id,
                username,
                real_name,
                email,
                stats["total_messages"] or 0,
                json.dumps(common_topics),
                stats["resolved"] or 0,
                stats["unresolved"] or 0,
                stats["avg_time"] or 0.0,
                knowledge_level
            ))
    
    def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        """Get user profile."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM user_profiles WHERE user_id = ?
            """, (user_id,))
            
            row = cursor.fetchone()
            if not row:
                return None
            
            row_dict = dict(row)
            return UserProfile(
                user_id=row_dict["user_id"],
                username=row_dict["username"],
                real_name=row_dict["real_name"],
                email=row_dict["email"],
                total_messages=row_dict["total_messages"],
                common_topics=json.loads(row_dict["common_topics"]) if row_dict["common_topics"] else [],
                resolved_issues=row_dict["resolved_issues"],
                unresolved_issues=row_dict["unresolved_issues"],
                avg_resolution_time=row_dict["avg_resolution_time"],
                last_interaction=datetime.fromisoformat(row_dict["last_interaction"]) if row_dict["last_interaction"] else None,
                knowledge_level=row_dict["knowledge_level"]
            )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_conversations,
                    SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) as resolved_count,
                    SUM(CASE WHEN escalated = 1 THEN 1 ELSE 0 END) as escalated_count,
                    AVG(confidence) as avg_confidence,
                    AVG(processing_time) as avg_processing_time
                FROM conversations
            """)
            
            stats = dict(cursor.fetchone())
            
            cursor.execute("""
                SELECT COUNT(DISTINCT user_id) as unique_users
                FROM conversations
            """)
            
            stats.update(dict(cursor.fetchone()))
            
            return stats


# Global database instance
_db: Optional[Database] = None


def get_database(db_path: Optional[str] = None) -> Database:
    """Get or create the global database instance."""
    global _db
    if _db is None:
        from agent.config import Config
        _db = Database(db_path=db_path or Config.DATABASE_PATH)
    return _db
