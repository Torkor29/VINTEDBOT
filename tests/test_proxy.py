import base64
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from app.configure_proxy import main as configure
from app.core import ORIGIN
from app.proxy import VintedProxyHandler, proxy_url
from app.workers import RemoteError, VintedSession


class ProxyTests(unittest.TestCase):
    def test_validation_does_not_echo_credentials(self):
        for value in ['socks5://u:secret@host:80', 'http://u:secret@host:bad',
                      'http://host:80/path', 'http://host:80#secret', 'http://host',
                      'http://host:80\n', 'http://user@host:80']:
            with self.subTest(value=value), self.assertRaises(ValueError) as e:
                proxy_url(value)
            self.assertNotIn('secret', str(e.exception))
        self.assertEqual(proxy_url(''), '')

    def test_explicit_proxy_ignores_no_proxy_and_sets_connect_target(self):
        value = 'http://user:p%40ss@proxy.example:8080'
        handler = VintedProxyHandler(value)
        req = Request(ORIGIN + '/robots.txt')
        with patch.dict(os.environ, {'NO_PROXY': '*', 'no_proxy': '*'}):
            handler.https_open(req)
        self.assertEqual(req.host, 'proxy.example:8080')
        self.assertEqual(req._tunnel_host, 'www.vinted.fr')
        self.assertEqual(req.get_header('Proxy-authorization'),
                         'Basic ' + base64.b64encode(b'user:p@ss').decode())
        self.assertEqual(req.type, 'https')

    def test_real_connect_rejects_proxy_auth_without_direct_fallback(self):
        connects = []
        class RejectingProxy(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                connects.append((self.path, self.headers.get('Proxy-Authorization')))
                self.send_response(407)
                self.end_headers()
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), RejectingProxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            value = f'http://user:password@127.0.0.1:{server.server_port}'
            with patch.dict(os.environ, {'VINTED_PROXY_URL': value, 'NO_PROXY': '*'}):
                session = VintedSession()
                with self.assertRaises(RemoteError) as e:
                    session(ORIGIN + '/')
                self.assertEqual(e.exception.status, 407)
                self.assertTrue(session.proxy_enabled)
            self.assertEqual(connects, [('www.vinted.fr:443',
                'Basic ' + base64.b64encode(b'user:password').decode())])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_interactive_config_preserves_token_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                Path('.env').write_text('TELEGRAM_BOT_TOKEN=fixture\nVINTED_PROXY_URL=old\n')
                with patch('builtins.input', side_effect=['proxy.example', '8080']), \
                     patch('getpass.getpass', side_effect=['user', 'p@$ word']), \
                     patch('builtins.print') as output:
                    configure()
                content = Path('.env').read_text()
                self.assertIn('TELEGRAM_BOT_TOKEN=fixture', content)
                self.assertIn('user:p%40%24%20word@proxy.example:8080', content)
                self.assertEqual(Path('.env').stat().st_mode & 0o777, 0o600)
                self.assertNotIn('p@$', str(output.call_args_list))
            finally:
                os.chdir(previous)
