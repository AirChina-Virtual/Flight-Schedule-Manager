"""FlightRadarAPI wrapper (plan 6; step 3).

Only this module touches the third-party library ``fr24_work/FlightRadarAPI`` (read-only, never modified).
``vcca.config.ensure_flightradarapi_on_path()`` is called before importing ``FlightRadarAPI``.

Ported from:
  * legacy/fr24_auth.py               credential lookup (.env: season dir, fr24_work, ~), session cache ~/.fr24_session.json
  * legacy/fetch_flight_histories.py  ``fetch_one_flight`` (flight/list.json by flight or reg), ``parse_item`` row shape
  * legacy/fetch_airports.py          airport boards (``extract_row``)
  * legacy/fetch_search_index.py      search index

Normalised row shape (``FlightRow``; identical keys to legacy flight_histories.json rows, consumed by fr24_store):
  {"id", "number", "callsign", "airline_icao", "origin_icao", "origin_iata", "dest_icao", "dest_iata",
   "aircraft_code", "registration", "sched_dep", "sched_arr", "real_dep", "real_arr",
   "status_live", "status_text", "status_generic"}
  Timestamps int epoch seconds or None; codes upper-case strings ('' when absent); ``status_generic`` lower-case;
  ``status_live`` bool or None.

Error policy: HTTP 429 -> ``FR24RateLimited`` (caller halves its rate and backs off); 401/403 -> the client
re-logs in ONCE then raises ``FR24AuthError``; anything else -> ``FR24Error``. Never log credential values,
tokens or cookies (error messages carry the query and HTTP status only).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .config import Config

FlightRow = dict[str, Any]
QueryKind = Literal["flight", "reg"]

ROW_KEYS: tuple[str, ...] = (
    "id", "number", "callsign", "airline_icao", "origin_icao", "origin_iata", "dest_icao", "dest_iata",
    "aircraft_code", "registration", "sched_dep", "sched_arr", "real_dep", "real_arr",
    "status_live", "status_text", "status_generic",
)

#: string row fields that are codes (upper-cased); status_text keeps its case.
_CODE_KEYS = ("number", "callsign", "airline_icao", "origin_icao", "origin_iata", "dest_icao", "dest_iata",
              "aircraft_code", "registration")
_TS_KEYS = ("sched_dep", "sched_arr", "real_dep", "real_arr")

SESSION_CACHE_PATH = Path.home() / ".fr24_session.json"
SESSION_MAX_AGE_S = 6 * 3600
_ALLOWED_CODES = [400, 401, 403, 404, 429, 500, 502, 503, 504]


class FR24Error(Exception):
    """Generic FR24 failure (network, unexpected payload, 5xx)."""

    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class FR24AuthError(FR24Error):
    """Login failed / session rejected after one re-login attempt (401/403), or credentials missing."""


class FR24RateLimited(FR24Error):
    """HTTP 429. ``retry_after`` seconds if the server sent one."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, 429)
        self.retry_after = retry_after


@dataclass(slots=True)
class FetchResult:
    query: str                       # 'CA1234' | 'B-2447' | 'ZBAA'
    kind: str                        # 'flight' | 'reg' | 'board'
    http_status: int | None
    rows: list[FlightRow] = field(default_factory=list)
    more: bool = False               # FR24 reports more pages
    fetched_at: int = 0              # epoch seconds
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class AirportInfo:
    icao: str
    iata: str
    name: str


# ------------------------------------------------------------------ credentials / session cache ------------------------
def _load_env_creds(cfg: "Config | None") -> tuple[str | None, str | None]:
    """(user, password) from the first .env defining both (order per config.credential_env_candidates), else env vars.
    Values never leave this module except to FlightRadar24API.login."""
    from .config import credential_env_candidates

    for path in credential_env_candidates(cfg):
        if not path.is_file():
            continue
        vals: dict[str, str] = {}
        try:
            with open(path, encoding="utf-8-sig") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    vals[k.strip()] = v
        except OSError:
            continue
        if vals.get("FR24_USER") and vals.get("FR24_PASSWORD"):
            return vals["FR24_USER"], vals["FR24_PASSWORD"]
    u, p = os.environ.get("FR24_USER"), os.environ.get("FR24_PASSWORD")
    return (u, p) if u and p else (None, None)


def _load_session_cache(path: Path, max_age_s: int = SESSION_MAX_AGE_S, now: float | None = None) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("subscriptionKey"):
        return None
    if (now if now is not None else time.time()) - int(data.get("saved_ts") or 0) > max_age_s:
        return None
    return data


# ------------------------------------------------------------------ normalisation ------------------------------------
def _code(v: Any) -> str:
    return str(v).strip().upper() if v not in (None, "") else ""


def _ts(v: Any) -> int | None:
    if v in (None, "", 0):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _finish_row(row: dict[str, Any]) -> FlightRow:
    out: FlightRow = {}
    for k in ROW_KEYS:
        v = row.get(k)
        if k in _CODE_KEYS:
            v = _code(v)
        elif k in _TS_KEYS:
            v = _ts(v)
        elif k == "status_generic":
            v = str(v).strip().lower() if v not in (None, "") else ""
        elif k == "status_text":
            v = str(v) if v not in (None, "") else ""
        elif k == "status_live":
            v = None if v is None else bool(v)
        elif k == "id":
            v = str(v).strip() if v not in (None, "") else None
        out[k] = v
    return out


def normalize_flight_list_item(item: dict[str, Any]) -> FlightRow:
    """One raw flight/list.json item -> FlightRow (port of legacy fetch_flight_histories.parse_item)."""
    item = item or {}
    ident = item.get("identification") or {}
    al = (item.get("airline") or {}).get("code") or {}
    ac = item.get("aircraft") or {}
    ap = item.get("airport") or {}
    orig = (ap.get("origin") or {}).get("code") or {}
    dest = (ap.get("destination") or {}).get("code") or {}
    tm = item.get("time") or {}
    st = item.get("status") or {}
    return _finish_row({
        "id": ident.get("id"),
        "number": (ident.get("number") or {}).get("default"),
        "callsign": ident.get("callsign"),
        "airline_icao": al.get("icao"),
        "origin_icao": orig.get("icao"),
        "origin_iata": orig.get("iata"),
        "dest_icao": dest.get("icao"),
        "dest_iata": dest.get("iata"),
        "aircraft_code": (ac.get("model") or {}).get("code"),
        "registration": ac.get("registration"),
        "sched_dep": (tm.get("scheduled") or {}).get("departure"),
        "sched_arr": (tm.get("scheduled") or {}).get("arrival"),
        "real_dep": (tm.get("real") or {}).get("departure"),
        "real_arr": (tm.get("real") or {}).get("arrival"),
        "status_live": st.get("live"),
        "status_text": st.get("text"),
        "status_generic": ((st.get("generic") or {}).get("status") or {}).get("text"),
    })


def is_target_board_flight(fl: dict[str, Any], target_ops: tuple[str, ...], number_prefix: str = "CA") -> bool:
    """Legacy fetch_airports.is_target_flight: airline ICAO, callsign/number prefix, or IATA prefix."""
    al_code = (fl.get("airline") or {}).get("code") or {}
    ident = fl.get("identification") or {}
    al_icao = _code(al_code.get("icao"))
    al_iata = _code(al_code.get("iata"))
    cs = _code(ident.get("callsign"))
    num = _code((ident.get("number") or {}).get("default"))
    alt = _code((ident.get("number") or {}).get("alternative"))
    if al_icao in target_ops:
        return True
    if any(x.startswith(target_ops) for x in (cs, num, alt) if x):
        return True
    return al_iata == number_prefix or num.startswith(number_prefix)


def normalize_board_item(airport_icao: str, direction: str, item: dict[str, Any]) -> FlightRow:
    """One airport-board item (``{'flight': {...}}``) -> FlightRow (legacy fetch_airports.extract_row, row keys only)."""
    fl = (item or {}).get("flight") or {}
    row = normalize_flight_list_item(fl)
    if not row["origin_icao"] and direction == "departure":
        row["origin_icao"] = _code(airport_icao)
    if not row["dest_icao"] and direction == "arrival":
        row["dest_icao"] = _code(airport_icao)
    return row


def _retry_after(resp: Any) -> float | None:
    try:
        v = resp.get_headers().get("Retry-After")
        return float(v) if v else None
    except Exception:  # noqa: BLE001 - header parsing is best-effort
        return None


def _status_of_exc(e: BaseException) -> int | None:
    resp = getattr(e, "response", None)
    sc = getattr(resp, "status_code", None)
    if isinstance(sc, int):
        return sc
    s = str(e)
    for code in (401, 403, 429):
        if str(code) in s:
            return code
    return None


def _safe_err(e: BaseException) -> str:
    """Exception text without query strings (which carry the token)."""
    s = f"{type(e).__name__}: {e}"
    if "token=" in s:
        s = s.split("?", 1)[0] + " [url params removed]"
    return s[:300]


# ------------------------------------------------------------------ client -------------------------------------------
class FR24Client:
    """Logged-in FR24 session. Not thread-safe; rate limiting is the caller's job (fr24_fetch.TokenBucket)."""

    def __init__(self, cfg: "Config", *, timeout: int = 25, use_session_cache: bool = True,
                 session_cache_path: Path | None = None):
        from .config import ensure_flightradarapi_on_path

        ensure_flightradarapi_on_path()
        from FlightRadarAPI import FlightRadar24API  # noqa: PLC0415
        from FlightRadarAPI.core import Core  # noqa: PLC0415
        from FlightRadarAPI.request import RetryPolicy  # noqa: PLC0415

        self.cfg = cfg
        self.timeout = timeout
        self.use_session_cache = use_session_cache
        self.session_cache_path = Path(session_cache_path) if session_cache_path else SESSION_CACHE_PATH
        self._Core = Core
        self._api = FlightRadar24API(timeout=timeout, max_workers=1,
                                     retry=RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=15.0))
        self._lib_client = self._api._FlightRadar24API__client  # noqa: SLF001 - documented legacy access
        self._subkey: str = ""
        self.session_source: str = ""          # 'cache' | 'login' (for status lines; never the values)
        self.requests_sent: int = 0

    # ---- auth
    def login(self, *, force: bool = False) -> None:
        """Log in (or restore the cached session). Raises FR24AuthError when credentials are missing/invalid."""
        if not force and self.use_session_cache:
            cached = _load_session_cache(self.session_cache_path)
            if cached:
                self._restore_session(cached)
                self.session_source = "cache"
                return
        user, password = _load_env_creds(self.cfg)
        if not user or not password:
            raise FR24AuthError("FR24_USER / FR24_PASSWORD 未找到（.env 或环境变量）")
        try:
            self._api.login(user, password)
        except Exception as e:  # noqa: BLE001 - LoginError / HTTP errors
            raise FR24AuthError(f"FR24 登录失败: {type(e).__name__}", _status_of_exc(e)) from None
        finally:
            del user, password
        ud = self._api.get_login_data()
        self._subkey = str(ud.get("subscriptionKey") or "")
        if not self._subkey:
            raise FR24AuthError("FR24 登录成功但没有 subscriptionKey（账号非 Gold?）")
        self.session_source = "login"
        if self.use_session_cache:
            self._save_session(ud)

    def _restore_session(self, cached: dict) -> None:
        jar = self._lib_client._APIClient__session.cookies  # noqa: SLF001
        for c in cached.get("cookies") or []:
            try:
                jar.set(c["name"], c["value"], domain=c.get("domain") or ".flightradar24.com", path=c.get("path") or "/")
            except Exception:  # noqa: BLE001
                pass
        self._api._FlightRadar24API__login_data = {"userData": {  # noqa: SLF001
            "subscriptionKey": cached["subscriptionKey"],
            "accessToken": cached.get("accessToken", ""),
            "subscriptions": cached.get("subscriptions", {}),
            "features": cached.get("features", {}),
            "accountType": cached.get("accountType"),
        }}
        self._subkey = str(cached["subscriptionKey"])

    def _save_session(self, ud: dict) -> None:
        """Same file format as legacy fr24_auth._save_session_cache (so both tools share it)."""
        try:
            jar = self._lib_client._APIClient__session.cookies.jar  # noqa: SLF001
            cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in jar]
        except Exception:  # noqa: BLE001
            cookies = []
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
        try:
            tmp = self.session_cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.session_cache_path)
        except OSError:
            pass

    def _invalidate_session_cache(self) -> None:
        try:
            if self.session_cache_path.is_file():
                self.session_cache_path.unlink()
        except OSError:
            pass

    @property
    def is_logged_in(self) -> bool:
        return bool(self._subkey)

    # ---- raw request with status mapping + one re-login on 401/403
    def _get_json(self, url: str, params: dict[str, Any], what: str) -> tuple[int, dict]:
        if not self.is_logged_in:
            self.login()
        relogged = False
        while True:
            p = dict(params, token=self._subkey)
            self.requests_sent += 1
            try:
                resp = self._lib_client.request_standalone(url, params=p, headers=self._Core.json_headers,
                                                           allowed_error_codes=_ALLOWED_CODES, timeout=self.timeout)
            except Exception as e:  # noqa: BLE001 - CloudflareError / network / HTTPError
                sc = _status_of_exc(e)
                if sc == 429:
                    raise FR24RateLimited(f"{what}: HTTP 429") from None
                raise FR24Error(f"{what}: {_safe_err(e)}", sc) from None
            sc = resp.get_status_code()
            if sc in (401, 403):
                if relogged:
                    raise FR24AuthError(f"{what}: HTTP {sc}（已重新登录一次）", sc)
                relogged = True
                self._invalidate_session_cache()
                self.login(force=True)
                continue
            if sc == 429:
                raise FR24RateLimited(f"{what}: HTTP 429", _retry_after(resp))
            if sc != 200:
                raise FR24Error(f"{what}: HTTP {sc}", sc)
            try:
                data = resp.get_json_content() or {}
            except Exception as e:  # noqa: BLE001
                raise FR24Error(f"{what}: 响应不是 JSON ({type(e).__name__})", sc) from None
            return sc, data

    # ---- endpoints
    def flight_list(self, query: str, kind: QueryKind = "flight", *, page: int = 1, limit: int = 100) -> FetchResult:
        """flight/list.json for a flight number ('CA1234') or registration ('B-2447'). Page 1 only by default
        (plan 6.3: page 1 already reaches back before season start). Rows normalised to ``FlightRow``.
        Raises FR24RateLimited / FR24AuthError / FR24Error."""
        if kind not in ("flight", "reg"):
            raise ValueError(f"kind must be 'flight' or 'reg', not {kind!r}")
        q = str(query).strip().upper()
        url = f"{self._Core.api_flightradar_base_url}/flight/list.json"
        sc, data = self._get_json(url, {"query": q, "fetchBy": kind, "page": page, "limit": limit},
                                  f"flight/list {kind}={q}")
        res = (data.get("result") or {}).get("response") or {}
        items = res.get("data") or []
        if not isinstance(items, list):
            raise FR24Error(f"flight/list {kind}={q}: 意外的响应结构", sc)
        rows = [normalize_flight_list_item(it) for it in items if isinstance(it, dict)]
        more = bool((res.get("page") or {}).get("more"))
        return FetchResult(query=q, kind=kind, http_status=sc, rows=rows, more=more, fetched_at=int(time.time()))

    def _airport_payload(self, code: str, page: int, limit: int) -> tuple[int, dict]:
        url = self._Core.api_airport_data_url
        sc, data = self._get_json(url, {"format": "json", "code": code, "limit": limit, "page": page},
                                  f"airport {code} p{page}")
        return sc, (data.get("result") or {}).get("response") or {}

    def airport_board(self, icao: str, *, pages: int = 1) -> FetchResult:
        """Optional (--boards): arrivals+departures board rows for target operators, normalised to FlightRow.
        ``pages`` = number of forward pages (1, 2, ...); one request per page (the caller rate-limits per call,
        so keep pages=1 inside fr24_fetch)."""
        code = str(icao).strip().upper()
        rows: list[FlightRow] = []
        more = False
        sc = None
        for p in range(1, max(1, pages) + 1):
            sc, res = self._airport_payload(code, p, 100)
            sched = ((res.get("airport") or {}).get("pluginData") or {}).get("schedule") or {}
            for block_name, direction in (("arrivals", "arrival"), ("departures", "departure")):
                block = sched.get(block_name) or {}
                pg = block.get("page") or {}
                if int(pg.get("total") or 1) > p:
                    more = True
                for item in block.get("data") or []:
                    fl = (item or {}).get("flight") or {}
                    if is_target_board_flight(fl, self.cfg.target_ops, self.cfg.number_prefix):
                        rows.append(normalize_board_item(code, direction, item))
        return FetchResult(query=code, kind="board", http_status=sc, rows=rows, more=more, fetched_at=int(time.time()))

    def search(self, query: str, *, limit: int = 50) -> dict[str, Any]:
        """Optional (--search): raw FR24 search payload for a flight number / callsign
        ({'schedule': [...], 'live': [...], ...} as returned by FlightRadar24API.search)."""
        if not self.is_logged_in:
            self.login()
        self.requests_sent += 1
        try:
            return self._api.search(str(query).strip().upper(), limit=limit)
        except Exception as e:  # noqa: BLE001
            sc = _status_of_exc(e)
            if sc == 429:
                raise FR24RateLimited(f"search {query}: HTTP 429") from None
            if sc in (401, 403):
                raise FR24AuthError(f"search {query}: HTTP {sc}", sc) from None
            raise FR24Error(f"search {query}: {_safe_err(e)}", sc) from None

    def airport_info(self, code: str) -> AirportInfo | None:
        """Name/IATA lookup for AIRPORT_ADD rows (plan 5.8 D). None when FR24 does not know the airport."""
        c = str(code).strip().upper()
        try:
            _, res = self._airport_payload(c, 1, 1)
        except FR24Error as e:
            if e.http_status in (400, 404):
                return None
            raise
        det = ((res.get("airport") or {}).get("pluginData") or {}).get("details") or {}
        codes = det.get("code") or {}
        if not det:
            return None
        return AirportInfo(icao=_code(codes.get("icao")) or c, iata=_code(codes.get("iata")),
                           name=str(det.get("name") or "").strip())

    def close(self) -> None:
        self._subkey = ""

    def __enter__(self) -> "FR24Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def credentials_available(cfg: "Config | None" = None) -> bool:
    """True when FR24_USER and FR24_PASSWORD are found (never returns/prints the values).
    Implemented: thin wrapper over config.credential_status."""
    from .config import credential_status

    return credential_status(cfg)["source"] is not None
