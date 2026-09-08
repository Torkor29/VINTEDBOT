import json
import tempfile
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, HTTPCookieProcessor, build_opener
from urllib.response import addinfourl

from app.core import ORIGIN, Store
from app.workers import Collector, NoRedirect, VintedSession


class FixtureTransport(HTTPSHandler):
    def __init__(self, cookie=True, catalog_status=200):
        super().__init__()
        self.calls = []
        self.cookie = cookie
        self.catalog_status = catalog_status
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
        elif self.catalog_status != 200:
            raise HTTPError(req.full_url, self.catalog_status, 'refus', Message(), BytesIO(b''))
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


class ExpiredTokenHandling(unittest.TestCase):
    """Un jeton anonyme périmé se renouvelle ; un refus persistant reste un arrêt."""

    def due(self, store):
        """Ramène le filtre et le budget global à échéance, sans toucher au reste."""
        store.set('next_request', 0)
        with store.db() as c:
            c.execute('UPDATE filters SET next_poll=0')

    def collector(self, store, transport):
        collector = Collector(store, enabled=True)
        collector.session.opener = build_opener(NoRedirect,
            HTTPCookieProcessor(collector.session.cookies), transport)
        return collector

    def test_bounded_renewal_then_permanent_halt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db')
            store.save_filter(42, {'name': 'Nike', 'url': ORIGIN + '/catalog?search_text=nike',
                                   'interval': 15})
            transport = FixtureTransport(catalog_status=401)
            collector = self.collector(store, transport)
            for _ in range(20):
                self.due(store)
                collector.step()
            homes = [c for c in transport.calls if c[0] == ORIGIN + '/']
            self.assertEqual(len(homes), 4)  # session initiale + 3 renouvellements
            self.assertEqual(store.get('session_renewals')[0], 3)
            self.assertIn('401', store.get('collector_halted'))
            self.due(store)
            before = len(transport.calls)
            collector.step()
            self.assertEqual(len(transport.calls), before)

    def test_expired_cookie_is_replaced_before_the_next_catalogue_call(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db')
            store.save_filter(42, {'name': 'Nike', 'url': ORIGIN + '/catalog?search_text=nike',
                                   'interval': 15})
            transport = FixtureTransport()
            collector = self.collector(store, transport)
            for _ in range(3):
                self.due(store)
                collector.step()
            self.assertTrue(collector.session.ready)
            collector.session.cookies.clear()
            self.due(store)
            collector.step()
            self.assertEqual(transport.calls[-1][0], ORIGIN + '/')
            self.assertTrue(collector.session.ready)
            self.assertFalse(store.get('collector_halted', ''))
