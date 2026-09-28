import json
import os
from urllib.request import urlopen


port = os.environ.get("FAMILYOS_PORT", "8080")
with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
    payload = json.load(response)
if response.status != 200 or payload.get("status") != "ok":
    raise SystemExit(1)

