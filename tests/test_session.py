import json
import tempfile
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.request import HTTPSHandler, HTTPCookieProcessor, build_opener
from urllib.response import addinfourl

from app.core import ORIGIN, Store
from app.workers import Collector, NoRedirect, VintedSession


class FixtureTransport(HTTPSHandler):
    def __init__(self, cookie=True):
        super().__init__()
        self.calls = []
        self.cookie = cookie
        self.item = 1

    def https_open(self, req):
        self.calls.append((req.full_url, req.get_header('Cookie')))
        headers = Message()
        if req.full_url.endswith('/robots.txt'):
            body = b'User-agent: *\nAllow: /\n'
        elif req.full_url == ORIGIN + '/':
            if self.cookie:
                headers['Set-Cookie'] = 'access_token_web=fixture; Path=/; Secure; HttpOnly'
            body = b'<html></html>'
        else:
            body = json.dumps({'items': [{'id': self.item, 'title': 'Nike',
                    'price': {'amount': '5.00', 'currency_code': 'EUR'}}]}).encode()
        response = addinfourl(BytesIO(body), headers, req.full_url, 200)
        response.msg = 'OK'
        return response


class SessionIntegration(unittest.TestCase):
    def test_cookie_roundtrip_budget_baseline_and_new_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db')
            store.save_filter(42, {'name': 'Nike', 'url': ORIGIN + '/catalog?search_text=nike', 'interval': 15})
            collector = Collector(store, enabled=True)
            transport = FixtureTransport()
            collector.session.opener = build_opener(NoRedirect, HTTPCookieProcessor(collector.session.cookies), transport)
            with patch('app.workers.time.time') as clock:
                for instant in (1000, 1000.5, 1001, 1001.5, 1002):
                    clock.return_value = instant
                    collector.step()
                self.assertEqual(len(transport.calls), 3)
                self.assertIsNone(transport.calls[1][1])
                self.assertEqual(transport.calls[2][1], 'access_token_web=fixture')
                self.assertEqual(store.alerts(42), [])
                transport.item = 2
                clock.return_value = 1017
                collector.step()
                self.assertEqual(len(transport.calls), 4)
                self.assertEqual(transport.calls[3][1], 'access_token_web=fixture')
                self.assertEqual(len(store.alerts(42)), 1)
                self.assertNotIn('fixture', json.dumps(store.get('collector_last_attempt')))

    def test_missing_cookie_and_foreign_origin(self):
        session = VintedSession()
        transport = FixtureTransport(cookie=False)
        session.opener = build_opener(NoRedirect, HTTPCookieProcessor(session.cookies), transport)
        with self.assertRaises(ValueError):
            session(ORIGIN + '/')
        self.assertFalse(session.ready)
        with self.assertRaises(ValueError):
            session('https://example.com/')
        self.assertEqual(len(transport.calls), 1)
