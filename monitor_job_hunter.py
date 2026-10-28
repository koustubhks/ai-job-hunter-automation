"""Alert once when the scheduled Job Hunter cycle has stopped completing."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "reports" / "last_run_summary.json"
MARKER = ROOT / "reports" / "monitor_state.json"
MAX_AGE = timedelta(hours=30)


def main() -> None:
    load_dotenv(ROOT / ".env")
    now = datetime.now()
    report = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
    finished = report.get("finished_at", "")
    try:
        last_run = datetime.fromisoformat(finished)
    except (TypeError, ValueError):
        last_run = None

    if last_run and now - last_run <= MAX_AGE:
        MARKER.unlink(missing_ok=True)
        return

    marker = json.loads(MARKER.read_text(encoding="utf-8")) if MARKER.exists() else {}
    incident = finished or "no-successful-run"
    if marker.get("alerted_for") == incident:
        return

    webhook = os.getenv("DISCORD_WEBHOOK", "").strip()
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK is not configured")
    message = ("Job Hunter has not completed a cycle in over 30 hours. "
               f"Last saved completion: {finished or 'none'}. Check Task Scheduler and logs.")
    response = requests.post(webhook, json={"content": message}, timeout=15)
    response.raise_for_status()
    MARKER.parent.mkdir(exist_ok=True)
    temporary = MARKER.with_suffix(".tmp")
    temporary.write_text(json.dumps({"alerted_for": incident, "alerted_at": now.isoformat()}),
                         encoding="utf-8")
    temporary.replace(MARKER)


if __name__ == "__main__":
    main()
