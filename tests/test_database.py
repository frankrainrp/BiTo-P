from pathlib import Path
import sqlite3
import tempfile
import unittest

from familyos.db import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.temp.name) / "familyos.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_migrations_are_idempotent_and_enable_safety_pragmas(self):
        self.assertEqual(self.database.migrate(), 24)
        self.assertEqual(self.database.migrate(), 24)
        with self.database.session() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_member_requires_a_real_household(self):
        self.database.migrate()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.session() as connection:
                connection.execute(
                    "INSERT INTO members(id,household_id,display_name,role,is_admin,created_at,username,username_key) "
                    "VALUES ('m', 'missing', '成员', 'adult', 0, datetime('now'), 'member', 'member')"
                )

    def test_health_reports_schema(self):
        self.database.migrate()
        self.assertEqual(self.database.health(), {"status": "ok", "database": "ok", "schema_version": 24})


if __name__ == "__main__":
    unittest.main()
