"""SQLite journal of every signal the strategy produced and what the engine did with it."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                setup_id TEXT PRIMARY KEY,
                created INTEGER, bar_time INTEGER, symbol TEXT, direction INTEGER,
                entry REAL, stop REAL, target REAL, volume REAL, risk_money REAL,
                status TEXT, detail TEXT, reasons TEXT, ticket INTEGER
            )""")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(signals)")}
        for name, kind in (("score", "INTEGER"), ("score_parts", "TEXT")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE signals ADD COLUMN {name} {kind}")
        self.db.commit()

    def seen(self, setup_id: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM signals WHERE setup_id=?", (setup_id,)).fetchone() is not None

    def record(self, **row) -> None:
        row["reasons"] = json.dumps(row.get("reasons", []))
        row["score_parts"] = json.dumps(row.get("score_parts", []))
        cols = ",".join(row)
        with self.lock:
            self.db.execute(f"INSERT OR REPLACE INTO signals ({cols}) VALUES ({','.join('?' * len(row))})",
                            list(row.values()))
            self.db.commit()

    def opened_since(self, since: int) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM signals WHERE status='opened' AND created>=?",
                                   (since,)).fetchone()[0]

    def by_ticket(self, ticket: int) -> dict | None:
        with self.lock:
            cur = self.db.execute("SELECT * FROM signals WHERE ticket=?", (ticket,))
            row = cur.fetchone()
            return dict(zip([c[0] for c in cur.description], row)) if row else None

    def recent(self, limit: int = 50) -> list[dict]:
        with self.lock:
            cur = self.db.execute("SELECT * FROM signals ORDER BY created DESC LIMIT ?", (limit,))
            names = [c[0] for c in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
            r["score_parts"] = json.loads(r["score_parts"] or "[]")
        return rows
