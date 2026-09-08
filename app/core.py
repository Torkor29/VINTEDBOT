"""Persistent domain model. Amounts are integer euro cents, never binary floats."""
import csv
import io
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

ORIGIN = "https://www.vinted.fr"
QUERY_KEYS = {"search_text", "catalog[]", "brand_ids[]", "size_ids[]", "status_ids[]",
              "color_ids[]", "material_ids[]", "price_from", "price_to", "currency"}


def money(value):
    try:
        n = Decimal(str(value).replace(",", "."))
        if not n.is_finite() or n < 0 or n > 1000000 or n * 100 != (n * 100).to_integral_value():
            raise ValueError()
        return int(n * 100)
    except (InvalidOperation, ValueError):
        raise ValueError("Montant invalide : utilisez au plus deux décimales, entre 0 et 1 000 000 €.")


def clean_text(value, maximum=180, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError("Texte manquant ou trop long.")
    return value.strip()


def catalog_url(value):
    p = urlsplit(clean_text(value, 3000))
    if p.scheme != "https" or p.netloc != "www.vinted.fr" or p.path.rstrip("/") != "/catalog" or p.fragment:
        raise ValueError("Collez une recherche https://www.vinted.fr/catalog?... depuis Vinted France.")
    pairs = parse_qsl(p.query, keep_blank_values=False, max_num_fields=100)
    unknown = {k for k, _ in pairs} - QUERY_KEYS - {"order", "page", "per_page", "time"}
    if unknown:
        raise ValueError("Paramètre de recherche non pris en charge : " + ", ".join(sorted(unknown)))
    pairs = [(k, v) for k, v in pairs if k in QUERY_KEYS]
    if any(v != "EUR" for k, v in pairs if k == "currency"):
        raise ValueError("Seuls les prix en EUR sont pris en charge.")
    for k, v in pairs:
        if k in ("price_from", "price_to"):
            money(v)
    pairs = [(k, v) for k, v in pairs if k != "currency"] + [("currency", "EUR"), ("order", "newest_first")]
    return ORIGIN + "/catalog?" + urlencode(sorted(pairs))


def item_url(value):
    if not value:
        return ""
    p = urlsplit(value)
    if p.scheme != "https" or p.netloc != "www.vinted.fr" or not p.path.startswith("/items/"):
        raise ValueError("Lien d’article Vinted France invalide.")
    return ORIGIN + p.path


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, chat_id INTEGER);
            CREATE TABLE IF NOT EXISTS filters(
              id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
              url TEXT NOT NULL, exclude TEXT NOT NULL DEFAULT '', interval INTEGER NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, initialized INTEGER NOT NULL DEFAULT 0,
              next_poll REAL NOT NULL DEFAULT 0, last_poll REAL, error TEXT NOT NULL DEFAULT '',
              revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS seen(
              filter_id INTEGER REFERENCES filters(id) ON DELETE CASCADE,
              item_id TEXT, PRIMARY KEY(filter_id,item_id));
            CREATE TABLE IF NOT EXISTS alerts(
              id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, item_id TEXT NOT NULL,
              filter_name TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
              price_cents INTEGER NOT NULL, size TEXT NOT NULL, brand TEXT NOT NULL,
              created REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
              message_id INTEGER, UNIQUE(user_id,item_id));
            CREATE TABLE IF NOT EXISTS trades(
              id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
              url TEXT NOT NULL, bought_on TEXT NOT NULL, sold_on TEXT,
              purchase_cents INTEGER NOT NULL, purchase_fees_cents INTEGER NOT NULL,
              sale_cents INTEGER NOT NULL, sale_fees_cents INTEGER NOT NULL,
              refund_cents INTEGER NOT NULL, notes TEXT NOT NULL, created REAL NOT NULL,
              updated REAL NOT NULL);
            ''')

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    def get(self, key, default=None):
        with self.db() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(r[0]) if r else default

    def set(self, key, value):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def filters(self, user):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM filters WHERE user_id=? ORDER BY id DESC", (user,))]

    def save_filter(self, user, data, fid=None):
        name = clean_text(data.get("name"))
        url = catalog_url(data.get("url"))
        exclude = clean_text(data.get("exclude", ""), 500, False)
        interval = int(data.get("interval", 15))
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool) or not 15 <= interval <= 86400:
            raise ValueError("La fréquence doit être comprise entre 15 secondes et 24 heures.")
        with self.db() as c:
            if fid is None:
                if c.execute("SELECT count(*) FROM filters WHERE user_id=?", (user,)).fetchone()[0] >= 20:
                    raise ValueError("Limite de 20 filtres atteinte.")
                return c.execute("INSERT INTO filters(user_id,name,url,exclude,interval,enabled) VALUES (?,?,?,?,?,?)",
                                 (user, name, url, exclude, interval, enabled)).lastrowid
            old = c.execute("SELECT * FROM filters WHERE id=? AND user_id=?", (fid, user)).fetchone()
            if not old:
                raise LookupError()
            reset = old["url"] != url or old["exclude"] != exclude or (enabled and not old["enabled"])
            c.execute("UPDATE filters SET name=?,url=?,exclude=?,interval=?,enabled=?,initialized=?,next_poll=0,error='',revision=revision+1 WHERE id=?",
                      (name, url, exclude, interval, enabled, 0 if reset else old["initialized"], fid))
            if reset:
                c.execute("DELETE FROM seen WHERE filter_id=?", (fid,))
            return fid

    def delete(self, table, user, rid):
        if table not in ("filters", "trades"):
            raise ValueError()
        with self.db() as c:
            if not c.execute(f"DELETE FROM {table} WHERE id=? AND user_id=?", (rid, user)).rowcount:
                raise LookupError()

    def ingest(self, filt, items):
        """Snapshot and outbox commit together; discard responses to obsolete filters."""
        now = time.time()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT * FROM filters WHERE id=?", (filt["id"],)).fetchone()
            if not current or current["revision"] != filt["revision"] or not current["enabled"]:
                return
            for it in items:
                fresh = c.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (filt["id"], it["id"])).rowcount
                if not fresh or not current["initialized"]:
                    continue
                words = [w.strip().casefold() for w in current["exclude"].split(",") if w.strip()]
                if any(w in (it["title"] + " " + it["brand"]).casefold() for w in words):
                    continue
                c.execute("""INSERT OR IGNORE INTO alerts(user_id,item_id,filter_name,title,url,price_cents,size,brand,created)
                          VALUES (?,?,?,?,?,?,?,?,?)""", (filt["user_id"], it["id"], current["name"], it["title"],
                          it["url"], it["price_cents"], it["size"], it["brand"], now))
            c.execute("UPDATE filters SET initialized=1,last_poll=?,error='' WHERE id=?", (now, filt["id"]))

    def alerts(self, user):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM alerts WHERE user_id=? ORDER BY id DESC LIMIT 200", (user,))]

    def save_trade(self, user, data, tid=None):
        title = clean_text(data.get("title"))
        url = item_url(clean_text(data.get("url", ""), 2000, False))
        bought = date.fromisoformat(data.get("bought_on", "")).isoformat()
        sold = date.fromisoformat(data["sold_on"]).isoformat() if data.get("sold_on") else None
        if sold and sold < bought:
            raise ValueError("La vente ne peut pas précéder l’achat.")
        values = [money(data.get(k, "0")) for k in ("purchase", "purchase_fees", "sale", "sale_fees", "refund")]
        if not sold and any(values[2:]):
            raise ValueError("Renseignez une date de vente pour les montants de vente/remboursement.")
        notes = clean_text(data.get("notes", ""), 2000, False)
        with self.db() as c:
            args = (title, url, bought, sold, *values, notes, time.time())
            if tid:
                if not c.execute("""UPDATE trades SET title=?,url=?,bought_on=?,sold_on=?,purchase_cents=?,
                    purchase_fees_cents=?,sale_cents=?,sale_fees_cents=?,refund_cents=?,notes=?,updated=?
                    WHERE id=? AND user_id=?""", (*args, tid, user)).rowcount:
                    raise LookupError()
                return tid
            return c.execute("""INSERT INTO trades(title,url,bought_on,sold_on,purchase_cents,purchase_fees_cents,
                     sale_cents,sale_fees_cents,refund_cents,notes,updated,user_id,created)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (*args, user, time.time())).lastrowid

    def trades(self, user):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM trades WHERE user_id=? ORDER BY bought_on DESC,id DESC", (user,))]


def summary(trades):
    sold = [r for r in trades if r["sold_on"]]
    costs = lambda r: r["purchase_cents"] + r["purchase_fees_cents"]
    receipts = lambda r: r["sale_cents"] - r["sale_fees_cents"] - r["refund_cents"]
    return {"stock_count": len(trades) - len(sold),
            "stock_cents": sum(costs(r) for r in trades if not r["sold_on"]),
            "revenue_cents": sum(r["sale_cents"] for r in sold),
            "profit_cents": sum(receipts(r) - costs(r) for r in sold),
            "cashflow_cents": sum(receipts(r) for r in sold) - sum(costs(r) for r in trades)}


def export_csv(trades):
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["ID", "Article", "Lien", "Achat le", "Vente le", "Achat EUR", "Frais achat EUR",
                     "Vente EUR", "Frais vente EUR", "Remboursement client EUR", "Marge EUR", "Notes"])
    def safe(text):
        text = str(text or "")
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text
    def eur(n):
        return f"{Decimal(n)/100:.2f}".replace(".", ",")
    for r in trades:
        amounts = [r[k] for k in ("purchase_cents", "purchase_fees_cents", "sale_cents", "sale_fees_cents", "refund_cents")]
        margin = amounts[2] - amounts[3] - amounts[4] - amounts[0] - amounts[1]
        writer.writerow([r["id"], safe(r["title"]), r["url"], r["bought_on"], r["sold_on"],
                         *map(eur, amounts), eur(margin) if r["sold_on"] else "", safe(r["notes"])])
    return "\ufeff" + out.getvalue()
