"""Save proxy settings interactively, without printing credentials or using shell history."""
import getpass
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from .proxy import proxy_url


def main():
    path = Path('.env')
    if not path.is_file() or path.is_symlink():
        raise ValueError('Lancez cette commande dans le dépôt contenant .env.')
    host = input('Hôte du proxy (sans http://) : ').strip()
    port = input('Port : ').strip()
    username = getpass.getpass('Identifiant (vide si aucun) : ')
    password = getpass.getpass('Mot de passe (vide si aucun) : ')
    auth = quote(username, safe='') + ':' + quote(password, safe='') + '@' if username or password else ''
    value = proxy_url('http://' + auth + host + ':' + port)
    content = path.read_text()
    line = 'VINTED_PROXY_URL=' + value
    content = re.sub(r'(?m)^VINTED_PROXY_URL=.*$', lambda _: line, content) if re.search(r'(?m)^VINTED_PROXY_URL=', content) else content.rstrip('\n') + '\n' + line + '\n'
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir='.', delete=False) as f:
            name = f.name
            os.chmod(name, 0o600)
            f.write(content)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
    print('Proxy enregistré. Aucun identifiant affiché. La collecte n’a pas été relancée.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, EOFError):
        raise SystemExit('Configuration non enregistrée : vérifiez le dossier, l’hôte, le port et les identifiants.') from None
