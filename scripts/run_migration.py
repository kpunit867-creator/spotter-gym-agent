"""Run: python scripts/run_migration.py supabase/migrations/002_xxx.sql — applies one SQL file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from app.config import DATABASE_URL  # noqa: E402

path = Path(sys.argv[1])
with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
    conn.execute(path.read_text())
print(f"✅ Applied {path.name}")
