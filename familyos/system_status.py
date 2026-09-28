from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from urllib.parse import urlparse


class SystemStatusService:
    """Read the small, host-generated status snapshot without exposing host APIs."""

    def __init__(self, snapshot_path: str | None):
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None

    def snapshot(self) -> dict:
        unavailable = {
            "available": False,
            "hostname": "homeserver",
            "updated_at": None,
            "metrics": {"cpu": None, "memory": None, "gpu": None, "disk": None},
            "gpu": {"name": "", "temperature_c": None, "memory_used_mb": None, "memory_total_mb": None},
            "services": [],
        }
        if not self.snapshot_path or not self.snapshot_path.is_file():
            return unavailable
        try:
            if self.snapshot_path.stat().st_size > 65536:
                return unavailable
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return unavailable
            updated_at = str(payload.get("updated_at", ""))
            datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            metrics = payload.get("metrics", {})
            gpu = payload.get("gpu", {})
            services = payload.get("services", [])
            if not isinstance(metrics, dict) or not isinstance(gpu, dict) or not isinstance(services, list):
                return unavailable
            safe_services = []
            for item in services[:32]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", ""))[:80].strip()
                status = str(item.get("status", "unknown"))[:20].strip()
                url = str(item.get("url", ""))[:240].strip()
                parsed = urlparse(url) if url else None
                if url and (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"192.168.50.10", "192.168.50.10", "100.64.0.10"}):
                    url = ""
                if name:
                    safe_services.append({"name": name, "status": status, "url": url})
            return {
                "available": True,
                "hostname": str(payload.get("hostname", "homeserver"))[:80],
                "updated_at": updated_at,
                "uptime_seconds": max(0, int(payload.get("uptime_seconds", 0))),
                "metrics": {key: self._percent(metrics.get(key)) for key in ("cpu", "memory", "gpu", "disk")},
                "gpu": {
                    "name": str(gpu.get("name", ""))[:120],
                    "temperature_c": self._number(gpu.get("temperature_c"), 0, 150),
                    "memory_used_mb": self._number(gpu.get("memory_used_mb"), 0, 1_000_000),
                    "memory_total_mb": self._number(gpu.get("memory_total_mb"), 0, 1_000_000),
                },
                "services": safe_services,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return unavailable

    @staticmethod
    def _number(value: object, minimum: float, maximum: float) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed < minimum or parsed > maximum:
            return None
        return round(parsed, 1)

    @classmethod
    def _percent(cls, value: object) -> float | None:
        return cls._number(value, 0, 100)
