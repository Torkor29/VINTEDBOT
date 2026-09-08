import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def telegram_user(raw, token, allowed, now=None):
    """Validate Telegram initData server-side; never trust initDataUnsafe."""
    if not token or not raw or len(raw) > 12000:
        raise PermissionError("Ouvrez l’application depuis Telegram.")
    try:
        pairs = parse_qsl(raw, strict_parsing=True, max_num_fields=30)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError()
        received = data.pop("hash")
        # Telegram's bot-token HMAC check excludes hash only (signature stays if present).
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        age = (time.time() if now is None else now) - int(data["auth_date"])
        if not hmac.compare_digest(expected, received) or not -30 <= age <= 3600:
            raise ValueError()
        uid = json.loads(data["user"])["id"]
        if type(uid) is not int or uid not in allowed:
            raise ValueError()
        return uid
    except (KeyError, ValueError, TypeError):
        raise PermissionError("Accès refusé ou session expirée. Fermez puis rouvrez la mini-app.") from None
