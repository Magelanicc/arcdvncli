import json
import os
import sys
import time

import requests

from . import __version__

REPO = "Magelanicc/arcdvncli"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
CHECK_INTERVAL = 24 * 3600  # cek maksimal sekali sehari


def _cache_path():
    return os.path.expanduser("~/.arcdvncli_update_check")


def _parse(v):
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


def _update_hint(tag):
    # Binary hasil PyInstaller tidak bisa di-update lewat pipx
    if getattr(sys, "frozen", False):
        return f"Download versi baru di: {RELEASES_URL}"
    return f"Update: pipx install --force git+https://github.com/{REPO}.git@{tag}"


def check_for_update():
    """Return pesan update (str) kalau ada versi baru, selain itu None. Tidak pernah raise."""
    if os.getenv("ARCDVN_NO_UPDATE_CHECK") == "1":
        return None
    try:
        cache = {}
        p = _cache_path()
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cache = json.load(f)

        latest = cache.get("latest")
        if time.time() - cache.get("checked_at", 0) > CHECK_INTERVAL:
            r = requests.get(
                f"https://api.github.com/repos/{REPO}/releases/latest", timeout=2
            )
            r.raise_for_status()
            latest = r.json()["tag_name"]
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"checked_at": time.time(), "latest": latest}, f)

        if latest and _parse(latest) > _parse(__version__):
            return f"Versi baru tersedia: {latest} (kamu v{__version__}). {_update_hint(latest)}"
    except Exception:
        pass
    return None