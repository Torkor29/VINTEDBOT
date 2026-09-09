"""Bounded live collector check, isolated from production data and Telegram."""
import json
import tempfile
import time
from pathlib import Path

from .core import ORIGIN, Store
from .workers import Collector


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'probe.db')
        store.save_filter(1, {'name': 'Probe', 'url': ORIGIN + '/catalog?search_text=nike', 'interval': 15})
        worker = Collector(store, enabled=True)
        deadline = time.monotonic() + 60
        for _ in range(3):
            delay = max(0, store.get('next_request', 0) - time.time())
            if time.monotonic() + delay >= deadline:
                break
            time.sleep(delay)
            worker.step()
            if store.get('collector_halted') or store.get('collector_error'):
                break
        success = bool(store.filters(1)[0]['initialized'])
        print(json.dumps({'success': success, 'proxy_enabled': worker.session.proxy_enabled,
                          'last_attempt': store.get('collector_last_attempt'),
                          'halt_reason': store.get('collector_halted', ''),
                          'error': store.get('collector_error', '')}, ensure_ascii=False, indent=2))
        return 0 if success else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValueError:
        raise SystemExit('Configuration du proxy invalide. Vérifiez VINTED_PROXY_URL.') from None
