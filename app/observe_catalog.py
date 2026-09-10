"""Two bounded catalogue samples with a secret-free structural report.

This is a diagnostic tool, not a packet capture. It never prints filter
parameters, cookies, proxy credentials, response bodies or item account data.
"""
import argparse
import json
import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.robotparser import RobotFileParser

from .core import ORIGIN, Store
from .workers import RemoteError, USER_AGENT, VintedSession


DATE_WORDS = ("created", "updated", "uploaded", "published", "date", "time", "timestamp")


def analyse(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Format catalogue inattendu")
    items = data["items"]
    if any(not isinstance(item, dict) or "id" not in item for item in items):
        raise ValueError("Article catalogue inattendu")
    date_fields = {}
    for item in items[:3]:
        for key, value in item.items():
            if any(word in key.casefold() for word in DATE_WORDS):
                example = value if isinstance(value, (str, int, float, bool, type(None))) else "(valeur complexe)"
                if isinstance(example, str):
                    example = example[:80]
                date_fields.setdefault(key, {"type": type(value).__name__, "example": example})
    return {
        "count": len(items),
        "ids": [str(item["id"]) for item in items],
        "top_level_keys": sorted(data),
        "item_keys": sorted({key for item in items[:3] for key in item}),
        "date_fields": date_fields,
    }


def public_sample(view, meta):
    return {
        "count": view["count"],
        "first_ids": view["ids"][:10],
        "top_level_keys": view["top_level_keys"],
        "item_keys": view["item_keys"],
        "date_fields": view["date_fields"],
        "transfer": meta,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Observe deux réponses catalogue sans afficher de secret.")
    parser.add_argument("--samples", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--interval", type=int, choices=range(5, 61), default=15)
    args = parser.parse_args(argv)
    store = Store(os.environ.get("DATABASE_PATH", "data/vintedbot.db"))
    with store.db() as db:
        row = db.execute("SELECT id,url FROM filters WHERE enabled=1 ORDER BY id LIMIT 1").fetchone()
    if not row:
        raise SystemExit("Aucun filtre actif à observer.")
    size = min(96, max(10, int(os.environ.get("VINTED_RESULTS_PER_POLL", "24"))))
    api = ORIGIN + "/api/v2/catalog/items?" + urlencode(
        parse_qsl(urlsplit(row["url"]).query) + [("page", "1"), ("per_page", str(size))]
    )
    session = VintedSession()
    robots_raw = session(ORIGIN + "/robots.txt")
    robots = RobotFileParser()
    robots.parse(robots_raw.splitlines())
    if not robots.can_fetch(USER_AGENT, api) or not robots.can_fetch(USER_AGENT, ORIGIN + "/"):
        raise SystemExit("Observation refusée par robots.txt.")
    session(ORIGIN + "/")
    views = []
    output = {"filter_id": row["id"], "requested_results": size, "samples": []}
    for index in range(args.samples):
        if index:
            time.sleep(args.interval)
        view = analyse(session(api))
        views.append(view)
        output["samples"].append(public_sample(view, dict(session.last_response_meta or {})))
    if len(views) > 1:
        previous, current = views[-2], views[-1]
        old = set(previous["ids"])
        new = [item for item in current["ids"] if item not in old]
        output["comparison"] = {
            "new_ids_count": len(new),
            "new_ids": new,
            "same_order_for_common_ids": [item for item in current["ids"] if item in old] ==
                                         [item for item in previous["ids"] if item in set(current["ids"])],
            "window_full": current["count"] >= size,
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RemoteError as error:
        raise SystemExit(f"Observation impossible : HTTP {error.status}.") from None
    except (OSError, ValueError, json.JSONDecodeError):
        raise SystemExit("Observation impossible : réponse réseau ou format inattendu.") from None
