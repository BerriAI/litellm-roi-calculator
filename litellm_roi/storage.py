import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "data.db"
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS estimates (cache_key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pulls (cache_key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL, value TEXT NOT NULL);
            """)
        self.path.chmod(0o600)

    def connection(self):
        return sqlite3.connect(self.path)

    def estimate(self, key: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM estimates WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_estimate(self, key: str, value: dict):
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO estimates VALUES (?, ?)", (key, json.dumps(value)))

    def pull(self, key: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM pulls WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_pull(self, key: str, value: dict):
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO pulls VALUES (?, ?)", (key, json.dumps(value)))

    def save_report(self, value: dict) -> dict:
        with self.connection() as conn:
            result = conn.execute("INSERT INTO reports (mode,value) VALUES (?,?)", (value["mode"], json.dumps(value)))
            value["id"] = result.lastrowid
        return value

    def latest(self, mode: str = "live") -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT id,value FROM reports WHERE mode=? ORDER BY id DESC LIMIT 1", (mode,)).fetchone()
        if not row:
            return None
        return {**json.loads(row[1]), "id": row[0]}

    def clear_reports(self):
        with self.connection() as conn:
            conn.execute("DELETE FROM reports WHERE mode='live'")
