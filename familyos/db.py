from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3


class Database:
    def __init__(self, path: str, migrations_dir: Path | None = None):
        self.path = path
        self.migrations_dir = migrations_dir or Path(__file__).parents[1] / "migrations"

    def connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def session(self):
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> int:
        with self.session() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            for path in sorted(self.migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version in applied:
                    continue
                connection.executescript(path.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                    (version,),
                )
            return connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]

    def health(self) -> dict:
        with self.session() as connection:
            connection.execute("SELECT 1").fetchone()
            version = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        return {"status": "ok", "database": "ok", "schema_version": version}

