from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).parents[1] / "agents" / "windows_activity" / "collector.py"
SPEC = importlib.util.spec_from_file_location("windows_activity_collector", MODULE_PATH)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class WindowsActivityAgentTests(unittest.TestCase):
    def row(self, start: str, end: str, app: str = "Code", category: str = "work") -> dict:
        return {"started_at": start, "ended_at": end, "app_name": app, "category": category}

    def test_overlap_is_not_double_counted(self):
        rows = [
            self.row("2026-09-03T09:00:00+00:00", "2026-09-03T09:05:00+00:00"),
            self.row("2026-09-03T09:04:00+00:00", "2026-09-03T09:10:00+00:00", "Terminal"),
        ]
        stats = collector.aggregate(rows)
        self.assertEqual(stats["observed_minutes"], 10.0)
        self.assertEqual(stats["sample_size"], 2)

    def test_redaction_and_classification(self):
        text = collector.redact_text("token=secret-value-12345 contact a@example.com")
        self.assertNotIn("secret-value", text)
        self.assertNotIn("a@example.com", text)
        self.assertEqual(collector.classify_activity("正在编程并运行测试"), "work")
        self.assertEqual(collector.classify_activity("Visual Studio Code", "播放视频"), "work")
        self.assertEqual(collector.classify_activity("Microsoft Edge", "正在看 YouTube"), "entertainment")

    def test_frame_fingerprint_distance(self):
        from PIL import Image, ImageDraw

        first = Image.new("RGB", (320, 180), "white")
        same = first.copy()
        changed = first.copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 170, 170), fill="black")
        try:
            first_hash = collector.image_fingerprint(first)
            self.assertEqual(collector.fingerprint_distance(first_hash, collector.image_fingerprint(same)), 0)
            self.assertGreater(collector.fingerprint_distance(first_hash, collector.image_fingerprint(changed)), 10)
        finally:
            first.close()
            same.close()
            changed.close()

    def test_collector_has_no_input_hooks_or_process_enumeration(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        forbidden = (
            "GetLastInputInfo", "GetWindowText", "SetWindowsHookEx", "EnumProcesses",
            "CreateToolhelp32Snapshot", "pynput", "keyboard", "mouse", "clipboard",
        )
        for marker in forbidden:
            self.assertNotIn(marker, source)

    def test_foreground_executable_is_normalized_without_window_title(self):
        self.assertEqual(collector.friendly_application_name(r"C:\\Program Files\\Microsoft VS Code\\Code.exe"), "Visual Studio Code")
        self.assertEqual(collector.friendly_application_name(r"C:\\Tools\\my_private_app.exe"), "my private app")

    def test_local_store_retains_only_text_not_images(self):
        with tempfile.TemporaryDirectory() as folder:
            store = collector.LocalStore(Path(folder) / "activity.db")
            event = {
                "client_event_id": "event-local-test", "source_device": "windows-main",
                "started_at": "2026-09-03T09:00:00+00:00", "ended_at": "2026-09-03T09:01:00+00:00",
                "app_name": "Code", "window_title": "", "ocr_excerpt": "tests",
                "activity_summary": "testing", "category": "work", "analysis_method": "windows_ocr",
                "analysis_confidence": 0.8,
            }
            store.add_event(event)
            with store.session() as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            self.assertFalse({"image", "screenshot", "pixels"}.intersection(columns))
            self.assertEqual(len(store.unsynced_events()), 1)

    def test_local_store_caches_executable_icons_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            store = collector.LocalStore(Path(folder) / "activity.db")
            store.save_icon("Visual Studio Code", "iVBORw0KGgo=")
            self.assertTrue(store.has_icon("Visual Studio Code"))
            self.assertEqual(len(store.unsynced_icons()), 1)
            store.mark_icon_synced("Visual Studio Code")
            self.assertEqual(store.unsynced_icons(), [])

    def test_agent_state_stores_only_compact_values(self):
        with tempfile.TemporaryDirectory() as folder:
            store = collector.LocalStore(Path(folder) / "activity.db")
            store.set_state("last_frame_fingerprint", "a1b2")
            self.assertEqual(store.get_state("last_frame_fingerprint"), "a1b2")

    def test_ollama_is_forced_to_loopback(self):
        self.assertEqual(
            collector.local_ollama_url({"ollama_url": "http://127.0.0.1:11434"}),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(collector.local_ollama_url({"ollama_url": "http://localhost:11434"}), "http://localhost:11434")
        with self.assertRaises(ValueError):
            collector.local_ollama_url({"ollama_url": "https://models.example.com"})

    def test_sensitive_foreground_apps_are_skipped_before_capture(self):
        config = {"sensitive_applications": ["Bitwarden.exe", "Windows Security"]}
        self.assertTrue(collector.is_sensitive_application("Bitwarden", r"C:\\Program Files\\Bitwarden\\Bitwarden.exe", config))
        self.assertTrue(collector.is_sensitive_application("Windows Security", None, config))
        self.assertFalse(collector.is_sensitive_application("Visual Studio Code", r"C:\\Program Files\\Code.exe", config))


if __name__ == "__main__":
    unittest.main()
