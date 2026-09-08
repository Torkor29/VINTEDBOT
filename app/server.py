"""Personal, single-instance service; run behind the supplied HTTPS reverse proxy."""
import fcntl
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .core import Store, export_csv, summary
from .security import telegram_user
from .workers import Collector, Telegram, run_loop

STATIC = Path(__file__).parent / "static"


def make_handler(store, config):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            pass  # Never log Telegram authentication data or user-entered values.

        def reply(self, code, body, kind="application/json; charset=utf-8", attachment=False):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            raw = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; object-src 'none'; form-action 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org")
            if attachment:
                self.send_header("Content-Disposition", 'attachment; filename="vintedbot-compta.csv"')
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Encodage non pris en charge.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 20000 or self.headers.get_content_type() != "application/json":
                raise ValueError("Corps JSON invalide ou trop volumineux.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Objet JSON attendu.")
            return data

        def dispatch(self):
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/healthz":
                return self.reply(200, {"ok": True})
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.command == "GET" and path in assets:
                filename, kind = assets[path]
                return self.reply(200, (STATIC / filename).read_bytes(), kind)
            if not path.startswith("/api/"):
                raise LookupError()
            origin = self.headers.get("Origin")
            if origin and origin != config["public_url"]:
                raise PermissionError("Origine non autorisée.")
            user = telegram_user(self.headers.get("X-Telegram-Init-Data", ""), config["token"], config["allowed"])
            if path == "/api/state" and self.command == "GET":
                trades = store.trades(user)
                with store.db() as c:
                    linked = c.execute("SELECT chat_id FROM users WHERE id=?", (user,)).fetchone()
                return self.reply(200, {"filters": store.filters(user), "alerts": store.alerts(user),
                    "trades": trades, "summary": summary(trades),
                    "status": {"collector_enabled": config["collector_enabled"],
                               "halted": store.get("collector_halted", ""),
                               "error": store.get("collector_error", ""),
                               "next_request": store.get("next_request", 0),
                               "telegram_linked": bool(linked and linked[0]),
                               "telegram_poll_ok": store.get("telegram_poll_ok", 0),
                               "global_gap": config["gap"]}})
            if path == "/api/export" and self.command == "GET":
                return self.reply(200, export_csv(store.trades(user)), "text/csv; charset=utf-8", True)
            bits = path.strip("/").split("/")
            if len(bits) in (2, 3) and bits[1] in ("filters", "trades"):
                table = bits[1]
                rid = int(bits[2]) if len(bits) == 3 else None
                if self.command == "POST" and rid is None:
                    save = store.save_filter if table == "filters" else store.save_trade
                    return self.reply(201, {"id": save(user, self.body())})
                if self.command == "PUT" and rid:
                    save = store.save_filter if table == "filters" else store.save_trade
                    return self.reply(200, {"id": save(user, self.body(), rid)})
                if self.command == "DELETE" and rid:
                    store.delete(table, user, rid)
                    return self.reply(200, {"ok": True})
            raise LookupError()

        def handle_request(self):
            try:
                self.dispatch()
            except PermissionError as e:
                self.reply(401, {"error": str(e)})
            except LookupError:
                self.reply(404, {"error": "Élément introuvable."})
            except (ValueError, TypeError, OverflowError):
                self.reply(400, {"error": "Valeurs invalides. Vérifiez les champs, les montants et le lien Vinted."})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logging.error("Erreur de requête ; détails confidentiels masqués.")
                self.reply(500, {"error": "Erreur serveur. Réessayez."})

        do_GET = do_POST = do_PUT = do_DELETE = handle_request
    return Handler


def configuration():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed = {int(x.strip()) for x in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip()}
    url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    parsed = urlsplit(url)
    if not token or not allowed or parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise SystemExit("Configurez TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS et PUBLIC_URL (origine HTTPS seule).")
    enabled = os.environ.get("VINTED_COLLECTION_ENABLED", "false").lower() == "true"
    if enabled and os.environ.get("VINTED_ACCESS_AUTHORIZED", "false").lower() != "true":
        raise SystemExit("Collecte désactivée : un accès autorisé doit être confirmé dans la configuration.")
    return {"token": token, "allowed": allowed, "public_url": url,
            "collector_enabled": enabled, "gap": max(60, int(os.environ.get("GLOBAL_REQUEST_GAP_SECONDS", "60")))}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = configuration()
    path = os.environ.get("DATABASE_PATH", "data/vintedbot.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    lock = open(path + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Une instance utilise déjà cette base de données.")
    store = Store(path)
    stop = threading.Event()
    collector = Collector(store, config["collector_enabled"], config["gap"])
    telegram = Telegram(store, config["token"], config["allowed"], config["public_url"])
    for action, pause in ((collector.step, 1), (telegram.poll, 1), (telegram.deliver, 1.1)):
        threading.Thread(target=run_loop, args=(stop, action, pause), daemon=True).start()
    server = ThreadingHTTPServer((os.environ.get("BIND_HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))), make_handler(store, config))
    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logging.info("VINTEDBOT démarré ; collecte %s", "activée" if collector.enabled else "désactivée")
    server.serve_forever()
    server.server_close()
    lock.close()


if __name__ == "__main__":
    main()
