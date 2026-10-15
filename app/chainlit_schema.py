"""Schema for Chainlit's SQLAlchemyDataLayer on SQLite.

Lives in its own file (data/chainlit.db): Chainlit's `users`/`threads` tables would collide with ours.
Column names are Chainlit's (quoted camelCase). Booleans are INTEGER, JSON is TEXT.
"""

import sqlite3
from pathlib import Path

DDL = [
    '''CREATE TABLE IF NOT EXISTS users (
        "id" TEXT PRIMARY KEY, "identifier" TEXT NOT NULL UNIQUE, "metadata" TEXT NOT NULL, "createdAt" TEXT)''',
    '''CREATE TABLE IF NOT EXISTS threads (
        "id" TEXT PRIMARY KEY, "createdAt" TEXT, "name" TEXT, "userId" TEXT, "userIdentifier" TEXT,
        "tags" TEXT, "metadata" TEXT, FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE)''',
    '''CREATE TABLE IF NOT EXISTS steps (
        "id" TEXT PRIMARY KEY, "name" TEXT NOT NULL, "type" TEXT NOT NULL, "threadId" TEXT NOT NULL,
        "parentId" TEXT, "streaming" INTEGER NOT NULL, "waitForAnswer" INTEGER, "isError" INTEGER,
        "metadata" TEXT, "tags" TEXT, "input" TEXT, "output" TEXT, "createdAt" TEXT, "command" TEXT,
        "start" TEXT, "end" TEXT, "generation" TEXT, "showInput" TEXT, "language" TEXT, "indent" INTEGER,
        "defaultOpen" INTEGER, "autoCollapse" INTEGER, "modes" TEXT, "icon" TEXT,
        FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE)''',
    '''CREATE TABLE IF NOT EXISTS elements (
        "id" TEXT PRIMARY KEY, "threadId" TEXT, "type" TEXT, "chainlitKey" TEXT, "url" TEXT, "objectKey" TEXT,
        "name" TEXT NOT NULL, "display" TEXT, "size" TEXT, "language" TEXT, "page" INTEGER, "autoPlay" INTEGER,
        "playerConfig" TEXT, "forId" TEXT, "mime" TEXT, "props" TEXT,
        FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE)''',
    '''CREATE TABLE IF NOT EXISTS feedbacks (
        "id" TEXT PRIMARY KEY, "forId" TEXT NOT NULL, "threadId" TEXT NOT NULL, "value" INTEGER NOT NULL, "comment" TEXT,
        FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE)''',
    'CREATE INDEX IF NOT EXISTS ix_steps_thread ON steps("threadId")',
    'CREATE INDEX IF NOT EXISTS ix_threads_user ON threads("userId")',
]


# Columns Chainlit 2.12 writes that older docs omit. Added to existing files too.
EXTRA_COLUMNS = {"steps": [("autoCollapse", "INTEGER"), ("modes", "TEXT"), ("icon", "TEXT")]}


def ensure_chainlit_schema(path: str = "data/chainlit.db") -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        for stmt in DDL:
            con.execute(stmt)
        for table, cols in EXTRA_COLUMNS.items():
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {typ}')
        con.commit()
    finally:
        con.close()
