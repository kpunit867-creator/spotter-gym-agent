"""Run: python scripts/check_db.py — confirms the database connection and tables."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Connection errors can quote parts of the URL; keep them out of the output.
logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)

import psycopg  # noqa: E402

from app.config import DATABASE_URL  # noqa: E402
from app.db import EXPECTED_TABLES  # noqa: E402

if not DATABASE_URL:
    print("❌ DATABASE_URL is empty — fill it in the .env file.")
    sys.exit(1)

try:
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
        version = conn.execute("show server_version").fetchone()[0]
        found = {r[0] for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'public'")}
except Exception as e:
    # Show the kind of error only, never the URL or password.
    msg = str(e).splitlines()[0] if str(e) else type(e).__name__
    if "password" in msg.lower() or "percent" in msg.lower():
        msg = "authentication / password problem (details hidden)"
    print(f"❌ Could not connect: {type(e).__name__}: {msg}")
    sys.exit(1)

print(f"✅ Connected (Postgres {version})")
print(f"Tables: {len(found & EXPECTED_TABLES)}/{len(EXPECTED_TABLES)}")
missing = sorted(EXPECTED_TABLES - found)
if missing:
    print("Missing:", ", ".join(missing))
    sys.exit(1)
