"""Safe credential loader and cached FR24 Gold session helper without exposing secrets in logs."""

import json
import os
import time
from typing import Any, Dict, Optional, Tuple
from FlightRadarAPI import FlightRadar24API
from FlightRadarAPI.request import RetryPolicy
from fr24_paths import SEASON_DIR, WORK_DIR

SESSION_CACHE_PATH = os.path.join(os.path.expanduser("~"), ".fr24_session.json")


def _candidate_env_paths():
    return [
        os.path.join(SEASON_DIR, ".env"),
        os.path.join(WORK_DIR, ".env"),
        os.path.expanduser("~/.env"),
    ]


def _load_env_creds() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (env_path, user, password) from the first .env defining both FR24_USER and FR24_PASSWORD."""
    for env_path in _candidate_env_paths():
        if not os.path.isfile(env_path):
            continue
        vals = {}
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
                    v = v[1:-1]
                vals[k] = v
        u = vals.get("FR24_USER")
        p = vals.get("FR24_PASSWORD")
        if u and p:
            return env_path, u, p
    u = os.environ.get("FR24_USER")
    p = os.environ.get("FR24_PASSWORD")
    if u and p:
        return "ENV", u, p
    return None, None, None


def credentials_available() -> bool:
    """Quietly verify whether FR24 credentials exist without exposing their values."""
    _, u, p = _load_env_creds()
    return bool(u and p)


def _save_session_cache(fr: FlightRadar24API) -> Dict[str, Any]:
    """Persist only subscriptionKey, non-PII feature metadata, and session cookies to ~/.fr24_session.json."""
    ud = fr.get_login_data()
    client = fr._FlightRadar24API__client
    jar = client._APIClient__session.cookies.jar
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in jar
    ]
    payload = {
        "saved_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "saved_ts": int(time.time()),
        "subscriptionKey": ud.get("subscriptionKey", ""),
        "accessToken": ud.get("accessToken", ""),
        "subscriptions": ud.get("subscriptions", {}),
        "features": ud.get("features", {}),
        "accountType": ud.get("accountType"),
        "cookies": cookies,
    }
    with open(SESSION_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return payload


def _load_session_cache(max_age_sec: int = 21600) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(SESSION_CACHE_PATH):
        return None
    try:
        data = json.load(open(SESSION_CACHE_PATH, "r", encoding="utf-8"))
        if int(time.time()) - int(data.get("saved_ts") or 0) > max_age_sec:
            return None
        if not data.get("subscriptionKey"):
            return None
        return data
    except Exception:
        return None


def create_logged_in_api(
    timeout: int = 25,
    max_workers: int = 2,
    retry: Optional[RetryPolicy] = None,
    force_refresh: bool = False,
) -> Tuple[FlightRadar24API, Dict[str, Any]]:
    """Create a FlightRadar24API client backed by cached or fresh login session (returns (api, session_meta))."""
    if retry is None:
        retry = RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=15.0)
    fr = FlightRadar24API(timeout=timeout, max_workers=max_workers, retry=retry)

    cached = None if force_refresh else _load_session_cache()
    if cached:
        client = fr._FlightRadar24API__client
        jar = client._APIClient__session.cookies
        for c in cached.get("cookies") or []:
            jar.set(c["name"], c["value"], domain=c.get("domain") or ".flightradar24.com", path=c.get("path") or "/")
        fr._FlightRadar24API__login_data = {
            "userData": {
                "subscriptionKey": cached["subscriptionKey"],
                "accessToken": cached.get("accessToken", ""),
                "subscriptions": cached.get("subscriptions", {}),
                "features": cached.get("features", {}),
                "accountType": cached.get("accountType"),
            }
        }
        return fr, cached

    _, user, password = _load_env_creds()
    if not user or not password:
        raise RuntimeError("FR24_USER / FR24_PASSWORD not found in .env or environment.")
    fr.login(user, password)
    meta = _save_session_cache(fr)
    return fr, meta
