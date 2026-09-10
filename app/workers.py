"""Single collector, persistent global limits, and durable Telegram outbox."""
import gzip
import json
import logging
import random
import time
from io import BytesIO
from http.cookiejar import CookieJar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPCookieProcessor
from urllib.robotparser import RobotFileParser

from .core import ORIGIN, clean_text, item_url, money
from .proxy import VintedProxyHandler, proxy_url

log = logging.getLogger(__name__)
USER_AGENT = "VintedBotPersonal/0.1"
MAX_RESPONSE_BYTES = 2_000_000


class RemoteError(Exception):
    def __init__(self, status, retry_after=0):
        self.status, self.retry_after = status, retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_seconds(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 0


def request(url, payload=None, timeout=20):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    try:
        with build_opener(NoRedirect).open(Request(url, data=body, headers=headers), timeout=timeout) as response:
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError("Réponse trop volumineuse")
            return data.decode("utf-8")
    except HTTPError as e:
        retry = retry_seconds(e.headers.get("Retry-After"))
        # Telegram generally puts retry_after in JSON, not the HTTP header.
        if url.startswith("https://api.telegram.org/"):
            try:
                retry = max(retry, int(json.loads(e.read(10000)).get("parameters", {}).get("retry_after", 0)))
            except (ValueError, TypeError):
                pass
        raise RemoteError(e.code, retry) from None


def normalize_items(raw):
    """Undocumented public catalogue format: fail closed on schema changes."""
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Format catalogue inattendu")
    items = []
    for r in data["items"]:
        if not isinstance(r, dict):
            raise ValueError("Article invalide")
        iid = str(r["id"])
        if not iid.isdigit():
            raise ValueError("Identifiant invalide")
        price = r["price"]
        if not isinstance(price, dict) or price.get("currency_code") != "EUR":
            raise ValueError("Format prix ou devise non pris en charge")
        if r.get("is_reserved") or r.get("is_closed"):
            continue
        items.append({"id": iid, "title": clean_text(r["title"], 500),
                      "url": item_url(r.get("url") or f"{ORIGIN}/items/{iid}"),
                      "price_cents": money(price["amount"]),
                      "size": clean_text(r.get("size_title") or "", 100, False),
                      "brand": clean_text(r.get("brand_title") or "", 100, False)})
    return items


class VintedSession:
    """One anonymous, in-memory session; never import account cookies."""
    def __init__(self):
        self.cookies = CookieJar()
        self.proxy_enabled = bool(proxy_url())
        self.opener = build_opener(VintedProxyHandler(proxy_url()), NoRedirect,
                                  HTTPCookieProcessor(self.cookies))
        self.ready = False
        self.last_response_meta = None

    def valid(self):
        """Le cookie anonyme expire ; ne jamais réutiliser un jeton périmé."""
        self.cookies.clear_expired_cookies()
        return any(c.name == "access_token_web" and not c.is_expired() for c in self.cookies)

    def invalidate(self):
        self.cookies.clear()
        self.ready = False

    def __call__(self, url):
        if urlsplit(url).scheme != "https" or urlsplit(url).netloc != urlsplit(ORIGIN).netloc:
            raise ValueError("Origine de session invalide")
        homepage = url == ORIGIN + "/"
        # Aucune usurpation d'identité : agent réel, langue du catalogue demandé.
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9",
                   "Accept-Encoding": "gzip",
                   "Accept": "text/html" if homepage else "application/json"}
        started = time.monotonic()
        try:
            with self.opener.open(Request(url, headers=headers), timeout=20) as response:
                if homepage:
                    self.ready = self.valid()
                    if not self.ready:
                        raise ValueError("Cookie de session absent")
                    self.last_response_meta = {"compressed_body_bytes": 0, "decoded_bytes": 0,
                                               "encoding": response.headers.get("Content-Encoding", "identity"),
                                               "duration_ms": round((time.monotonic() - started) * 1000)}
                    return ""
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("Réponse trop volumineuse")
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding == "gzip":
                    with gzip.GzipFile(fileobj=BytesIO(raw)) as packed:
                        decoded = packed.read(MAX_RESPONSE_BYTES + 1)
                elif encoding in ("", "identity"):
                    decoded = raw
                else:
                    raise ValueError("Compression de réponse inattendue")
                if len(decoded) > MAX_RESPONSE_BYTES:
                    raise ValueError("Réponse décompressée trop volumineuse")
                self.last_response_meta = {"compressed_body_bytes": len(raw), "decoded_bytes": len(decoded),
                                           "encoding": encoding or "identity",
                                           "duration_ms": round((time.monotonic() - started) * 1000)}
                return decoded.decode("utf-8")
        except HTTPError as e:
            status, retry = e.code, retry_seconds(e.headers.get("Retry-After"))
            e.close()
            raise RemoteError(status, retry) from None
        except URLError as e:
            # urllib surfaces CONNECT failures as URLError, not HTTPError.
            if str(e.reason).startswith('Tunnel connection failed: 407'):
                raise RemoteError(407) from None
            raise


class Collector:
    def __init__(self, store, enabled=False, gap=1, fetch=None, page_size=24, rebaseline_after=120):
        self.session = VintedSession() if fetch is None else None
        self.store, self.enabled, self.gap, self.fetch = store, enabled, max(1, gap), fetch or self.session
        self.page_size = min(96, max(10, int(page_size)))
        self.rebaseline_after = max(60, int(rebaseline_after))
        self.robots = None
        self.robots_until = 0

    def request_gap(self):
        delay = self.robots.crawl_delay(USER_AGENT) or 0
        rate = self.robots.request_rate(USER_AGENT)
        return max(self.gap, delay, rate.seconds / rate.requests if rate else 0)

    def record_attempt(self, stage, outcome, http_status=None):
        # No search parameters, response bodies, cookies, tokens or user IDs.
        self.store.set("collector_last_attempt", {"stage": stage, "outcome": outcome,
                       "http_status": http_status, "at": time.time()})

    def record_transfer(self, stage):
        if self.session is None or not self.session.last_response_meta:
            return
        meta = {"stage": stage, "at": time.time(), **self.session.last_response_meta}
        self.store.set("collector_last_transfer", meta)
        totals = self.store.get("collector_transfer_totals", {"compressed_body_bytes": 0, "decoded_bytes": 0,
                                                                "responses": 0})
        for key in ("compressed_body_bytes", "decoded_bytes"):
            totals[key] = int(totals.get(key, 0)) + int(meta[key])
        totals["responses"] = int(totals.get("responses", 0)) + 1
        self.store.set("collector_transfer_totals", totals)

    def halt(self, stage, message):
        self.store.set("collector_halted", message)
        log.warning("Collecte arrêtée (%s) : %s", stage, message)

    def step(self):
        now = time.time()
        if not self.enabled or self.store.get("collector_halted") or self.store.get("next_request", 0) > now:
            return
        with self.store.db() as c:
            row = c.execute("SELECT * FROM filters WHERE enabled=1 AND next_poll<=? ORDER BY next_poll,id LIMIT 1", (now,)).fetchone()
        if not row:
            return
        f = dict(row)
        api = ORIGIN + "/api/v2/catalog/items?" + urlencode(parse_qsl(urlsplit(f["url"]).query) +
                                                              [("page", "1"), ("per_page", str(self.page_size))])
        # Persist before network I/O; restarts never reset the global budget.
        self.store.set("next_request", now + self.gap)
        stage = "robots.txt" if self.robots_until <= now else "catalogue"
        try:
            if self.robots_until <= now:
                self.record_attempt(stage, "requesting")
                raw = self.fetch(ORIGIN + "/robots.txt")
                self.record_transfer(stage)
                if "user-agent:" not in raw.lower():
                    raise ValueError("robots.txt illisible")
                self.robots = RobotFileParser()
                self.robots.parse(raw.splitlines())
                self.robots_until = now + 3600
                # robots.txt also consumes one request slot.
                self.store.set("next_request", now + self.request_gap())
                self.record_attempt(stage, "ok", 200)
                return
            if not self.robots.can_fetch(USER_AGENT, api):
                self.record_attempt("robots.txt", "disallowed")
                self.halt("robots.txt", "Accès refusé par robots.txt")
                return
            self.store.set("next_request", now + self.request_gap())
            if self.session is not None and not (self.session.ready and self.session.valid()):
                self.session.invalidate()
                stage = "session"
                if not self.robots.can_fetch(USER_AGENT, ORIGIN + "/"):
                    self.record_attempt(stage, "disallowed")
                    self.halt(stage, "Accueil refusé par robots.txt")
                    return
                self.record_attempt(stage, "requesting")
                self.fetch(ORIGIN + "/")
                self.record_transfer(stage)
                self.record_attempt(stage, "ok", 200)
                return  # Session creation consumes its own global request slot.
            with self.store.db() as c:
                c.execute("UPDATE filters SET next_poll=? WHERE id=?", (now + f["interval"], f["id"]))
            self.record_attempt(stage, "requesting")
            items = normalize_items(self.fetch(api))
            self.record_transfer(stage)
            silent = bool(f["initialized"] and f["last_poll"] and now - f["last_poll"] > self.rebaseline_after)
            result = self.store.ingest(f, items, notify=not silent)
            if result is None:
                return
            self.store.set("collector_window", {"items": len(items), "limit": self.page_size,
                "new_items": result["new_items"], "alerts_created": result["alerts_created"],
                "new_prefix": result["new_prefix"], "anchor_status": result["anchor_status"],
                "silent_rebaseline": silent,
                "saturated": result["anchor_status"] == "missing",
                "at": now})
            self.record_attempt(stage, "ok", 200)
            self.store.set("collector_failures", 0)
            self.store.set("collector_error", "")
        except RemoteError as e:
            self.record_attempt(stage, "http_error", e.status)
            if e.status == 401 and stage == "catalogue" and self.renew_session(now):
                self.backoff(f, f"Session Vinted expirée à l’étape {stage} (HTTP 401) ; renouvellement.", e.retry_after)
            elif e.status in (401, 403, 407) or 300 <= e.status < 400:
                reason = ("Authentification du proxy refusée" if e.status == 407 else
                          "Redirection non suivie" if 300 <= e.status < 400 else "Accès refusé")
                self.halt(stage, f"{reason} à l’étape {stage} (HTTP {e.status}). Aucune nouvelle tentative automatique.")
            else:
                self.backoff(f, f"Vinted répond HTTP {e.status} à l’étape {stage}", e.retry_after)
        except (ValueError, KeyError, TypeError):
            self.record_attempt(stage, "format_error")
            self.halt(stage, f"Réponse inattendue à l’étape {stage}. Vérification manuelle nécessaire.")
        except OSError:
            self.record_attempt(stage, "network_error")
            self.backoff(f, f"Vinted injoignable à l’étape {stage}")

    def renew_session(self, now):
        """Un jeton anonyme périmé se renouvelle ; un refus persistant reste un arrêt.

        Budget strict : au plus 3 renouvellements par heure. Ce n’est pas une
        rotation d’identité et cela ne contourne aucun blocage.
        """
        if self.session is None:
            return False
        count, since = self.store.get("session_renewals", [0, 0])
        if now - since > 3600:
            count, since = 0, now
        if count >= 3:
            return False
        self.store.set("session_renewals", [count + 1, since])
        self.session.invalidate()
        return True

    def backoff(self, f, message, retry=0):
        failures = min(self.store.get("collector_failures", 0) + 1, 10)
        delay = max(retry, min(3600, 60 * 2 ** failures)) + random.uniform(0, 15)
        self.store.set("collector_failures", failures)
        self.store.set("collector_error", message)
        self.store.set("next_request", time.time() + delay)
        with self.store.db() as c:
            c.execute("UPDATE filters SET error=? WHERE id=?", (message, f["id"]))


class Telegram:
    def __init__(self, store, token, allowed, public_url, fetch=request, max_age=120):
        self.store, self.token, self.allowed, self.public_url, self.fetch = store, token, allowed, public_url, fetch
        self.max_age = max(30, int(max_age))

    def call(self, method, data):
        response = json.loads(self.fetch(f"https://api.telegram.org/bot{self.token}/{method}", data, timeout=40))
        if not response.get("ok"):
            raise RemoteError(response.get("error_code", 500), response.get("parameters", {}).get("retry_after", 0))
        return response["result"]

    def poll(self):
        updates = self.call("getUpdates", {"offset": self.store.get("telegram_offset", 0), "timeout": 25,
                                          "allowed_updates": ["message"]})
        for update in updates:
            self.handle_update(update)
            self.store.set("telegram_offset", update["update_id"] + 1)
        self.store.set("telegram_poll_ok", time.time())

    def handle_update(self, update):
        msg = update.get("message", {})
        uid = msg.get("from", {}).get("id")
        chat = msg.get("chat", {})
        if uid not in self.allowed or chat.get("type") != "private" or chat.get("id") != uid:
            return
        command = msg.get("text", "").split(" ")[0].split("@")[0]
        if command in ("/start", "/app"):
            with self.store.db() as c:
                c.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (uid, uid))
            self.call("sendMessage", {"chat_id": uid, "text": "VINTEDBOT — tes filtres, tes annonces et ta compta.",
                      "reply_markup": {"inline_keyboard": [[{"text": "Ouvrir mon espace", "web_app": {"url": self.public_url}}]]}})
            self.call("setChatMenuButton", {"chat_id": uid, "menu_button": {"type": "web_app", "text": "Mon espace", "web_app": {"url": self.public_url}}})
        elif command == "/stop":
            with self.store.db() as c:
                c.execute("UPDATE users SET chat_id=NULL WHERE id=?", (uid,))
            self.call("sendMessage", {"chat_id": uid, "text": "Notifications suspendues. /start pour les réactiver."})

    def deliver(self):
        now = time.time()
        if not self.allowed or self.store.get("telegram_next_send", 0) > now:
            return
        with self.store.db() as c:
            c.execute("UPDATE alerts SET state='expired' WHERE state='pending' AND created<?", (now - self.max_age,))
            placeholders = ','.join('?' for _ in self.allowed)
            r = c.execute(f"""SELECT a.*,u.chat_id FROM alerts a JOIN users u ON u.id=a.user_id
                WHERE a.state='pending' AND a.next_try<=? AND u.chat_id IS NOT NULL
                AND a.user_id IN ({placeholders}) ORDER BY a.created DESC,a.id DESC LIMIT 1""",
                (now, *sorted(self.allowed))).fetchone()
        if not r:
            return
        text = f"{r['filter_name']}\n{r['title']}\n{r['price_cents']/100:.2f} € · {r['brand']} · {r['size']}"
        self.store.set("telegram_next_send", now + 1.1)
        try:
            result = self.call("sendMessage", {"chat_id": r["chat_id"], "text": text,
                               "reply_markup": {"inline_keyboard": [[{"text": "Voir sur Vinted", "url": r["url"]}]]}})
            with self.store.db() as c:
                c.execute("UPDATE alerts SET state='sent',message_id=? WHERE id=?", (result["message_id"], r["id"]))
        except RemoteError as e:
            if e.status == 403:
                with self.store.db() as c:
                    c.execute("UPDATE users SET chat_id=NULL WHERE id=?", (r["user_id"],))
            self.retry(r, e.retry_after, permanent=e.status == 400)
        except (OSError, ValueError, KeyError):
            self.retry(r)

    def retry(self, r, after=0, permanent=False):
        attempts = r["attempts"] + 1
        delay = max(after, min(3600, 2 ** min(attempts, 12)))
        self.store.set("telegram_next_send", time.time() + delay)
        with self.store.db() as c:
            c.execute("UPDATE alerts SET attempts=?,next_try=?,state=? WHERE id=?",
                      (attempts, time.time() + delay, "failed" if permanent or attempts >= 12 else "pending", r["id"]))


def run_loop(stop, action, pause):
    while not stop.is_set():
        try:
            action()
        except RemoteError as e:
            # Do not log raw exceptions: urllib errors may contain bot-token URLs.
            log.warning("Service distant HTTP %s", e.status)
            stop.wait(max(30, e.retry_after))
        except Exception:
            log.error("Erreur de worker ; aucune donnée sensible journalisée.")
            stop.wait(30)
        stop.wait(pause)
