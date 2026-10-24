"""Database connection pool (Postgres on Supabase) and small query helpers."""
import logging
import threading
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import DATABASE_URL

# Connection errors can quote parts of the URL (including the password).
logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)

EXPECTED_TABLES = {
    "users", "user_targets", "whatsapp_templates", "messages",
    "meals", "meal_items", "exercises", "workout_sessions", "workout_sets",
    "workout_plans", "workout_plan_days", "workout_plan_exercises",
    "weight_logs", "reminders", "agent_turns", "llm_calls",
    "conversation_state", "corrections",
}

_pool = None


class QueryStats(threading.local):
    """Per-thread count and time of database queries (read by the evaluation runner)."""
    def __init__(self):
        self.count = 0
        self.ms = 0.0

    def reset(self):
        self.count, self.ms = 0, 0.0


QUERY_STATS = QueryStats()


class TimedConnection(psycopg.Connection):
    def execute(self, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return super().execute(*args, **kwargs)
        finally:
            QUERY_STATS.count += 1
            QUERY_STATS.ms += (time.perf_counter() - t0) * 1000


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is empty — fill it in the .env file.")
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            connection_class=TimedConnection,
            open=True,
        )
    return _pool


def fetch_all(conn, sql, params=None):
    return conn.execute(sql, params or ()).fetchall()


def fetch_one(conn, sql, params=None):
    return conn.execute(sql, params or ()).fetchone()


def check_schema() -> dict:
    """Confirms we can connect and that every expected table exists."""
    with get_pool().connection() as conn:
        rows = fetch_all(conn, "select tablename from pg_tables where schemaname = 'public'")
        version = fetch_one(conn, "show server_version")["server_version"]
    found = {r["tablename"] for r in rows}
    return {
        "postgres_version": version,
        "tables_found": len(found & EXPECTED_TABLES),
        "tables_expected": len(EXPECTED_TABLES),
        "missing_tables": sorted(EXPECTED_TABLES - found),
    }
