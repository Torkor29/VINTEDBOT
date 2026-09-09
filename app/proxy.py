"""Explicit HTTP CONNECT proxy for Vinted; credentials never enter diagnostics."""
import base64
import os
from urllib.parse import urlsplit, unquote
from urllib.request import ProxyHandler


def proxy_url(value=None):
    value = os.environ.get('VINTED_PROXY_URL', '') if value is None else value
    if not value:
        return ''
    try:
        p = urlsplit(value)
        if (p.scheme != 'http' or not p.hostname or not p.port
                or p.path not in ('', '/') or p.query or p.fragment
                or any(c.isspace() for c in value)
                or bool(p.username) != bool(p.password)):
            raise ValueError()
        return value
    except ValueError:
        raise ValueError('VINTED_PROXY_URL invalide : proxy HTTP CONNECT avec hôte et port requis.') from None


class VintedProxyHandler(ProxyHandler):
    def __init__(self, value):
        value = proxy_url(value)
        super().__init__({'https': value} if value else {})

    def proxy_open(self, req, proxy, type):
        # Do not let NO_PROXY silently bypass an explicitly configured proxy.
        p = urlsplit(proxy)
        host = p.netloc.rsplit('@', 1)[-1]
        if p.username:
            auth = (unquote(p.username) + ':' + unquote(p.password)).encode('utf-8')
            req.add_unredirected_header('Proxy-Authorization',
                                       'Basic ' + base64.b64encode(auth).decode('ascii'))
        # HTTPSHandler moves Proxy-Authorization into CONNECT, not the origin request.
        req.set_proxy(host, 'http')
        return None
