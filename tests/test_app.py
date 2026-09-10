import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.core import Store, catalog_url, export_csv, money, summary
from app.diagnostics import report
from app.security import telegram_user
from app.server import configuration, make_handler
from app.workers import Collector, RemoteError, Telegram, normalize_items, retry_seconds

TOKEN = "123:test-token-not-a-real-secret"


def signed(uid=42, timestamp=None, **extra):
    fields = {"user": json.dumps({"id": uid, "first_name": "Test"}),
              "auth_date": str(int(time.time()) if timestamp is None else timestamp), **extra}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def raw_items(*ids):
    return json.dumps({"items": [{"id": i, "title": "Veste bleue", "price": {"amount": "12.50", "currency_code": "EUR"},
        "url": f"https://www.vinted.fr/items/{i}-veste", "brand_title": "Patagonia", "size_title": "M"} for i in ids]})


class Authentication(unittest.TestCase):
    def test_valid_signature_including_optional_signature_field(self):
        self.assertEqual(telegram_user(signed(signature="optional-ed25519"), TOKEN, {42}), 42)

    def test_tamper_expiry_future_duplicate_and_allowlist(self):
        cases = [signed().replace("Test", "Evil"), signed(timestamp=int(time.time())-3601),
                 signed(timestamp=int(time.time())+100), signed()+"&auth_date=1", signed(uid=99), ""]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(PermissionError):
                telegram_user(raw, TOKEN, {42})


class Domain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "app.db")
        self.filter_data = {"name":"Vestes", "url":"https://www.vinted.fr/catalog?search_text=veste&brand_ids[]=10", "interval":300}
        self.fid = self.store.save_filter(42, self.filter_data)

    def tearDown(self):
        self.tmp.cleanup()

    def filt(self):
        return self.store.filters(42)[0]

    def seed_alert(self):
        self.store.ingest(self.filt(), normalize_items(raw_items(1)))
        self.store.ingest(self.filt(), normalize_items(raw_items(2, 1)))

    def test_baseline_restart_and_cross_filter_dedup(self):
        self.seed_alert()
        self.store = Store(self.store.path)
        self.store.ingest(self.filt(), normalize_items(raw_items(2, 1)))
        self.assertEqual(len(self.store.alerts(42)), 1)
        self.store.save_filter(42, {**self.filter_data,"name":"Autre"})
        self.store.ingest(self.filt(), normalize_items(raw_items(1)))
        self.store.ingest(self.filt(), normalize_items(raw_items(2)))
        self.assertEqual(len(self.store.alerts(42)), 1)

    def test_stale_responses_and_exclusions(self):
        old = self.filt()
        self.store.save_filter(42, {**self.filter_data, "exclude":"bleue"}, self.fid)
        self.store.ingest(old, normalize_items(raw_items(1)))
        self.assertFalse(self.filt()["initialized"])
        self.store.ingest(self.filt(), normalize_items(raw_items(1)))
        self.store.ingest(self.filt(), normalize_items(raw_items(2)))
        self.assertEqual(self.store.alerts(42), [])

    def test_only_items_before_previous_head_are_alerted(self):
        filt = self.filt()
        self.store.ingest(filt, normalize_items(raw_items(100, 99)))
        result = self.store.ingest(self.filt(), normalize_items(raw_items(50, 49)))
        self.assertEqual(result["anchor_status"], "missing")
        self.assertEqual(self.store.alerts(42), [])
        result = self.store.ingest(self.filt(), normalize_items(raw_items(51, 50, 49)))
        self.assertEqual(result["anchor_status"], "found")
        self.assertEqual([a["item_id"] for a in self.store.alerts(42)], ["51"])

    def test_no_data_access_across_users(self):
        self.assertEqual(self.store.filters(99), [])
        with self.assertRaises(LookupError):
            self.store.delete("filters", 99, self.fid)
        with self.assertRaises(LookupError):
            self.store.save_filter(99, self.filter_data, self.fid)

    def test_url_validation_and_canonicalization(self):
        url = catalog_url(self.filter_data["url"] + "&order=relevance&page=3")
        self.assertIn("newest_first", url)
        self.assertIn("brand_ids%5B%5D=10", url)
        for bad in ["http://www.vinted.fr/catalog", "https://www.vinted.fr.evil.org/catalog", "https://localhost/catalog",
                    "https://www.vinted.fr@evil.org/catalog", "https://www.vinted.fr/catalog?currency=USD",
                    "https://www.vinted.fr/catalog?unexpected_filter=123"]:
            with self.subTest(url=bad), self.assertRaises(ValueError):
                catalog_url(bad)

    def test_cent_precision_profit_stock_and_csv(self):
        self.assertEqual(money("0,29"), 29)
        for bad in ("NaN", "Infinity", "-1", "0.001", "1000001"):
            with self.assertRaises(ValueError):
                money(bad)
        self.store.save_trade(42, {"title":"=1+1", "bought_on":"2026-09-01", "sold_on":"2026-09-08",
                                   "purchase":"10.01", "purchase_fees":"2.99", "sale":"30", "sale_fees":"3", "refund":"5", "notes":"  @SUM(1)"})
        self.store.save_trade(42, {"title":"Stock", "bought_on":"2026-09-02", "purchase":"7"})
        trades = self.store.trades(42)
        self.assertEqual(summary(trades), {"stock_count":1,"stock_cents":700,"revenue_cents":3000,"profit_cents":900,"cashflow_cents":200})
        csv = export_csv(trades)
        self.assertIn("'=1+1", csv)
        self.assertIn("'@SUM(1)", csv)
        self.assertIn("9,00", csv)
        self.assertEqual(self.store.trades(99), [])

    def test_sale_validation(self):
        with self.assertRaises(ValueError):
            self.store.save_trade(42, {"title":"a", "bought_on":"2026-09-08", "sold_on":"2026-09-01"})
        with self.assertRaises(ValueError):
            self.store.save_trade(42, {"title":"a", "bought_on":"2026-09-08", "sale":"1"})

    def test_fast_interval_validation_and_update_preserves_baseline(self):
        data = {**self.filter_data, "interval": 15}
        self.store.ingest(self.filt(), normalize_items(raw_items(1)))
        self.store.save_filter(42, data, self.fid)
        self.assertTrue(self.filt()["initialized"])
        self.assertEqual(self.filt()["interval"], 15)
        self.store.ingest(self.filt(), normalize_items(raw_items(2, 1)))
        self.assertEqual(len(self.store.alerts(42)), 1)
        for invalid in (0, 14, 86401):
            with self.assertRaises(ValueError):
                self.store.save_filter(42, {**data, "interval": invalid})
        self.store.save_filter(42, {"name": "Default", "url": data["url"]})
        self.assertEqual(self.filt()["interval"], 15)
        with patch.dict('os.environ', {"TELEGRAM_BOT_TOKEN": TOKEN,
                        "TELEGRAM_ALLOWED_USER_IDS": "42", "PUBLIC_URL": "https://demo.trycloudflare.com"}, clear=True):
            self.assertEqual(configuration()["gap"], 1)
            self.assertFalse(configuration()["collector_enabled"])

    def test_three_fast_filters_keep_fifteen_second_cadence_without_bursts(self):
        self.store.save_filter(42, {**self.filter_data, "interval": 15}, self.fid)
        for keyword in ('pull', 'jean'):
            self.store.save_filter(42, {**self.filter_data, "interval": 15,
                                      "url": f"https://www.vinted.fr/catalog?search_text={keyword}"})
        requests = []
        with patch('app.workers.time.time') as clock:
            clock.return_value = 1000
            def fetch(url):
                requests.append((clock.return_value, url))
                return "User-agent: *\nAllow: /\n" if url.endswith('robots.txt') else raw_items(1)
            worker = Collector(self.store, True, fetch=fetch)
            worker.step()  # robots at 1000; catalogue queries at 1001, 1002, 1003
            for instant in (1000.5, 1001, 1001, 1002, 1003, 1015.99, 1016, 1017, 1018):
                clock.return_value = instant
                worker.step()
            self.assertEqual([t for t, url in requests if '/api/' in url], [1001, 1002, 1003, 1016, 1017, 1018])
            self.assertEqual([url for _, url in requests[1:4]], [url for _, url in requests[4:7]])
            self.assertEqual(sorted(f['next_poll'] for f in self.store.filters(42)), [1031, 1032, 1033])

    def test_fast_schedule_still_honors_robots_and_restart_budget(self):
        self.store.save_filter(42, {**self.filter_data, "interval": 15}, self.fid)
        requests = []
        with patch('app.workers.time.time') as clock:
            clock.return_value = 1000
            def fetch(url):
                requests.append(url)
                return 'User-agent: *\nAllow: /\nCrawl-delay: 30\n' if url.endswith('robots.txt') else raw_items(1)
            worker = Collector(self.store, True, fetch=fetch)
            worker.step()
            self.assertEqual(self.store.get('next_request'), 1030)
            clock.return_value = 1015
            worker.step()
            Collector(Store(self.store.path), True, fetch=fetch).step()
            self.assertEqual(len(requests), 1)
            clock.return_value = 1030
            worker.step()
            self.assertEqual(len(requests), 2)
            self.assertEqual(self.store.get('next_request'), 1060)

    def test_collector_disabled_robots_budget_and_403_halt(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if url.endswith("robots.txt"):
                return "User-agent: *\nAllow: /\n"
            raise RemoteError(403)
        worker = Collector(self.store, fetch=fetch)
        worker.step()
        self.assertFalse(calls)
        worker.enabled = True
        worker.step()
        worker.step()
        self.assertEqual(len(calls), 1)
        self.store.set("next_request", 0)
        worker.step()
        self.assertIn("403", self.store.get("collector_halted"))
        self.assertEqual(self.store.get("collector_last_attempt")["stage"], "catalogue")
        self.assertEqual(self.store.get("collector_last_attempt")["http_status"], 403)
        self.store.set("next_request", 0)
        worker.step()
        self.assertEqual(len(calls), 2)

    def test_robots_http_denial_is_distinct_and_diagnostic_cannot_resume(self):
        calls = []
        def denied(url):
            calls.append(url)
            raise RemoteError(403)
        Collector(self.store, True, fetch=denied).step()
        before = self.store.get("collector_halted")
        diagnostic = report(self.store.path, True)
        self.assertEqual(diagnostic["last_attempt"]["stage"], "robots.txt")
        self.assertEqual(diagnostic["last_attempt"]["http_status"], 403)
        self.assertEqual(diagnostic["filters_with_success"], 0)
        self.assertNotIn("url", diagnostic["last_attempt"])
        self.assertEqual(self.store.get("collector_halted"), before)
        self.store.set("next_request", 0)
        Collector(Store(self.store.path), True, fetch=denied).step()
        self.assertEqual(len(calls), 1)
        self.store.set("collector_last_attempt", None)
        self.assertIsNone(report(self.store.path)["last_attempt"])
        self.assertEqual(report(self.store.path)["halt_reason"], before)

    def test_robots_denial_and_retry_after(self):
        worker = Collector(self.store, True, fetch=lambda url:"User-agent: *\nDisallow: /api/\n")
        worker.step()
        self.store.set("next_request", 0)
        worker.step()
        self.assertIn("robots", self.store.get("collector_halted"))
        self.store.set("collector_halted", "")
        self.store.set("next_request", 0)
        worker = Collector(self.store, True, fetch=lambda url:"User-agent: *\nAllow: /\n")
        worker.step()
        self.store.set("next_request", 0)
        def fail(url):
            raise RemoteError(429, 7200)
        worker.fetch = fail
        worker.step()
        self.assertGreaterEqual(self.store.get("next_request"), time.time()+7199)
        self.assertEqual(retry_seconds("120"), 120)

    def test_outbox_retry_then_success_and_private_chat_only(self):
        self.seed_alert()
        with self.store.db() as c:
            c.execute("INSERT INTO users VALUES(42,42)")
        worker = Telegram(self.store,TOKEN,{42},"https://bot.example")
        def fail(*_):
            raise RemoteError(429, 120)
        worker.call = fail
        worker.deliver()
        self.assertEqual(self.store.alerts(42)[0]["state"], "pending")
        self.assertEqual(self.store.alerts(42)[0]["attempts"], 1)
        self.assertGreater(self.store.get("telegram_next_send"), time.time()+119)
        calls=[]
        worker.call=lambda *args: calls.append(args) or {"message_id":55}
        worker.handle_update({"message":{"from":{"id":42},"chat":{"id":-1,"type":"group"},"text":"/start"}})
        self.assertFalse(calls)
        self.store.set("telegram_next_send",0)
        with self.store.db() as c:
            c.execute("UPDATE alerts SET next_try=0")
        worker.deliver()
        self.assertEqual(self.store.alerts(42)[0]["state"],"sent")
        self.assertEqual(calls[0][1]["chat_id"],42)

    def test_long_interruption_rebaselines_and_outbox_prioritizes_recent(self):
        self.store.save_filter(42, {**self.filter_data, "interval": 15}, self.fid)
        responses = iter([
            "User-agent: *\nAllow: /\n",
            raw_items(1),
            raw_items(3, 2, 1),
            raw_items(4, 3, 2),
        ])
        worker = Collector(self.store, True, fetch=lambda _: next(responses), rebaseline_after=120)
        with patch('app.workers.time.time') as clock:
            clock.return_value = 1000
            worker.step()
            clock.return_value = 1001
            worker.step()
            clock.return_value = 1016
            worker.step()
            self.assertEqual([a['item_id'] for a in self.store.alerts(42)], ['3', '2'])
            clock.return_value = 1200
            worker.step()
        self.assertEqual([a['item_id'] for a in self.store.alerts(42)], ['3', '2'])
        with self.store.db() as c:
            c.execute("INSERT INTO users VALUES(42,42)")
            current = time.time()
            c.execute("UPDATE alerts SET created=? WHERE item_id='3'", (current,))
            c.execute("UPDATE alerts SET created=? WHERE item_id='2'", (current - 500,))
        telegram = Telegram(self.store, TOKEN, {42}, "https://bot.example", max_age=120)
        calls = []
        telegram.call = lambda method, data: calls.append((method, data)) or {"message_id": 7}
        telegram.deliver()
        self.assertIn('3', calls[0][1]['reply_markup']['inline_keyboard'][0][0]['url'])
        states = {a['item_id']: a['state'] for a in self.store.alerts(42)}
        self.assertEqual(states, {'3': 'sent', '2': 'expired'})

    def test_format_change_never_accepted_as_empty_catalogue(self):
        for value in ['{}', '<html>challenge</html>', '{"items":{}}']:
            with self.assertRaises(ValueError):
                normalize_items(value)


class HttpIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/"db.sqlite")
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,{"token":TOKEN,"allowed":{42,99},"public_url":"https://bot.example","collector_enabled":False,"gap":60}))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def req(self,path,method='GET',body=None,auth=None,origin=None):
        headers={}
        if auth:headers['X-Telegram-Init-Data']=auth
        if origin:headers['Origin']=origin
        if body is not None:headers['Content-Type']='application/json'
        return urlopen(Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method))

    def test_authenticated_crud_export_and_isolation(self):
        with self.assertRaises(HTTPError) as e:
            self.req('/api/state')
        self.assertEqual(e.exception.code,401)
        body={'title':'Veste','purchase':'12.50','bought_on':'2026-09-08'}
        with self.req('/api/trades','POST',body,signed()) as r:
            tid=json.load(r)['id']
        with self.req('/api/state',auth=signed()) as r:
            self.assertEqual(json.load(r)['summary']['stock_cents'],1250)
        with self.req('/api/state',auth=signed(99)) as r:
            self.assertEqual(json.load(r)['trades'],[])
        with self.assertRaises(HTTPError) as e:
            self.req('/api/trades/'+str(tid),'DELETE',auth=signed(99))
        self.assertEqual(e.exception.code,404)
        with self.req('/api/export',auth=signed()) as r:
            self.assertIn('12,50',r.read().decode())
        with self.assertRaises(HTTPError) as e:
            self.req('/api/state',auth=signed(),origin='https://evil.example')
        self.assertEqual(e.exception.code,401)
        with self.req('/') as r:
            self.assertIn('Content-Security-Policy',r.headers)
        with self.req('/api/trades/'+str(tid),'DELETE',auth=signed()) as r:
            self.assertTrue(json.load(r)['ok'])


if __name__ == '__main__':
    unittest.main()
