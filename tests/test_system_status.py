import json
from pathlib import Path
import tempfile
import unittest

from familyos.system_status import SystemStatusService


class SystemStatusTests(unittest.TestCase):
    def test_snapshot_is_sanitized_and_limited_to_local_links(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "status.json"
            path.write_text(json.dumps({
                "hostname": "homeserver",
                "updated_at": "2026-09-04T14:00:00+00:00",
                "uptime_seconds": 500,
                "metrics": {"cpu": 12.3, "memory": 101, "gpu": 4, "disk": 10},
                "gpu": {"name": "RTX 3060", "temperature_c": 44, "memory_used_mb": 1200, "memory_total_mb": 12288},
                "services": [
                    {"name": "FamilyOS", "status": "healthy", "url": "https://192.168.50.10:8443/"},
                    {"name": "Bad", "status": "running", "url": "javascript:alert(1)"},
                ],
            }), encoding="utf-8")
            result = SystemStatusService(str(path)).snapshot()
        self.assertTrue(result["available"])
        self.assertIsNone(result["metrics"]["memory"])
        self.assertEqual(result["services"][0]["url"], "https://192.168.50.10:8443/")
        self.assertEqual(result["services"][1]["url"], "")

    def test_invalid_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "status.json"
            path.write_text("not-json", encoding="utf-8")
            result = SystemStatusService(str(path)).snapshot()
        self.assertFalse(result["available"])
        self.assertEqual(result["services"], [])


if __name__ == "__main__":
    unittest.main()
