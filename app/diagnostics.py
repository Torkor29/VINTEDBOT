"""Read-only local report: no requests to Vinted, no state reset, no secrets."""
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote


def report(path, enabled=False):
    uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
    c = sqlite3.connect(uri, uri=True)
    try:
        def setting(key, default=None):
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
        total, active, verified = c.execute("SELECT count(*),coalesce(sum(enabled),0),count(last_poll) FROM filters").fetchone()
        renewals, since = setting("session_renewals", [0, 0])
        return {"collector_enabled": enabled, "halt_reason": setting("collector_halted", ""),
                "last_error": setting("collector_error", ""),
                "last_attempt": setting("collector_last_attempt"),
                "next_request_at": setting("next_request", 0),
                "session_renewals_last_hour": renewals if time.time() - since <= 3600 else 0,
                "proxy_enabled": bool(os.environ.get("VINTED_PROXY_URL", "")),
                "last_transfer": setting("collector_last_transfer"),
                "transfer_totals": setting("collector_transfer_totals", {"compressed_body_bytes": 0,
                    "decoded_bytes": 0, "responses": 0}),
                "catalogue_window": setting("collector_window"),
                "filters_total": total, "filters_enabled": active, "filters_with_success": verified,
                "alerts_pending": c.execute("SELECT count(*) FROM alerts WHERE state='pending'").fetchone()[0],
                "alerts_expired": c.execute("SELECT count(*) FROM alerts WHERE state='expired'").fetchone()[0],
                "telegram_last_poll_at": setting("telegram_poll_ok", 0)}
    finally:
        c.close()


if __name__ == "__main__":
    try:
        result = report(os.environ.get("DATABASE_PATH", "data/vintedbot.db"),
                        os.environ.get("VINTED_COLLECTION_ENABLED", "false").lower() == "true")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (sqlite3.Error, ValueError):
        raise SystemExit("Diagnostic indisponible : vérifiez le chemin et les droits de lecture de la base.") from None
