"""Build the self-contained dashboard from the latest run results."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = Path(os.environ.get("LAUNCH_OUT_DIR", ROOT / "out"))
SOURCE = OUT / "results.json"
TARGET = Path(os.environ.get("LAUNCH_DASHBOARD_PATH", OUT / "index.html"))
if not SOURCE.exists():
    raise SystemExit(f"Results file not found: {SOURCE}. Run run_demo.py first.")
data = json.loads(SOURCE.read_text(encoding="utf-8"))
html = (ROOT / "dashboard_template.html").read_text(encoding="utf-8")
html = html.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
TARGET.parent.mkdir(parents=True, exist_ok=True)
TARGET.write_text(html, encoding="utf-8")
print(f"Dashboard built: {TARGET} ({len(html) // 1024} KB)")
