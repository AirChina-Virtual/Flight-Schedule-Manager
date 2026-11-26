"""fr24_client: row normalisation, board filtering, status -> typed error mapping (no network)."""

import tempfile
import time
import unittest
from pathlib import Path

from tests import _support as S  # noqa: F401  (sys.path)
from vcca import fr24_client as FC

RAW_ITEM = {
    "identification": {"id": "3ff0b56c", "number": {"default": "ca1002", "alternative": None}, "callsign": "CAO3049"},
    "status": {"live": False, "text": "Landed 10:30", "generic": {"status": {"text": "Landed"}}},
    "aircraft": {"model": {"code": "b77l", "text": "Boeing 777-F"}, "registration": "B-225A"},
    "airline": {"name": "Air China Cargo", "code": {"iata": "CA", "icao": "CAO"}},
    "airport": {"origin": {"code": {"iata": "PVG", "icao": "ZSPD"}}, "destination": {"code": {"iata": "LAX", "icao": "KLAX"}}},
    "time": {"scheduled": {"departure": 1780122960, "arrival": 1780160422},
             "real": {"departure": 1780123169, "arrival": "1780162233"}},
}


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status, self.payload, self.headers = status, payload or {}, headers or {}

    def get_status_code(self):
        return self.status

    def get_json_content(self):
        return self.payload

    def get_headers(self):
        return self.headers


class _LibClient:
    """Stands in for FlightRadarAPI's APIClient: returns queued responses, records params."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request_standalone(self, url, **kw):
        self.calls.append((url, kw["params"]))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client(responses, logins):
    """FR24Client with the library session replaced and login() stubbed (counts calls)."""
    c = FC.FR24Client.__new__(FC.FR24Client)
    c.cfg = S.cfg()
    c.timeout = 5
    c.use_session_cache = False
    c.session_cache_path = Path(tempfile.gettempdir()) / "vcca_test_no_session.json"

    class _Core:
        api_flightradar_base_url = "https://api.example/common/v1"
        api_airport_data_url = "https://api.example/common/v1/airport.json"
        json_headers = {}

    c._Core = _Core
    c._lib_client = _LibClient(responses)
    c._subkey = "k0"
    c.session_source = "test"
    c.requests_sent = 0

    def login(*, force=False):
        logins.append(force)
        c._subkey = f"k{len(logins)}"

    c.login = login
    return c


class NormalizeTest(unittest.TestCase):
    def test_flight_list_item(self):
        r = FC.normalize_flight_list_item(RAW_ITEM)
        self.assertEqual(tuple(r), FC.ROW_KEYS)
        self.assertEqual(r["id"], "3ff0b56c")
        self.assertEqual(r["number"], "CA1002")
        self.assertEqual(r["callsign"], "CAO3049")
        self.assertEqual(r["aircraft_code"], "B77L")
        self.assertEqual((r["origin_icao"], r["origin_iata"], r["dest_icao"], r["dest_iata"]), ("ZSPD", "PVG", "KLAX", "LAX"))
        self.assertEqual(r["real_arr"], 1780162233)          # str -> int
        self.assertEqual(r["status_generic"], "landed")
        self.assertIs(r["status_live"], False)

    def test_empty_item(self):
        r = FC.normalize_flight_list_item({})
        self.assertIsNone(r["id"])
        self.assertEqual(r["callsign"], "")
        self.assertIsNone(r["sched_dep"])
        self.assertIsNone(r["status_live"])

    def test_board_filter_and_fill(self):
        ops = ("CCA", "CAO", "CCD", "BJN")
        self.assertTrue(FC.is_target_board_flight(RAW_ITEM, ops))
        other = {"identification": {"callsign": "CES501", "number": {"default": "MU501"}},
                 "airline": {"code": {"icao": "CES", "iata": "MU"}}}
        self.assertFalse(FC.is_target_board_flight(other, ops))
        item = {"flight": {**RAW_ITEM, "airport": {"origin": {"code": {"iata": "PVG", "icao": "ZSPD"}}, "destination": {}}}}
        self.assertEqual(FC.normalize_board_item("ZBAA", "arrival", item)["dest_icao"], "ZBAA")


class ErrorMappingTest(unittest.TestCase):
    OK = {"result": {"response": {"data": [RAW_ITEM], "page": {"more": True}}}}

    def test_ok_params(self):
        logins = []
        c = _client([_Resp(200, self.OK)], logins)
        res = c.flight_list("ca1002", "flight")
        self.assertTrue(res.ok)
        self.assertTrue(res.more)
        self.assertEqual(len(res.rows), 1)
        url, params = c._lib_client.calls[0]
        self.assertTrue(url.endswith("/flight/list.json"))
        self.assertEqual((params["query"], params["fetchBy"], params["page"], params["limit"]), ("CA1002", "flight", 1, 100))
        self.assertEqual(logins, [])

    def test_429(self):
        c = _client([_Resp(429, headers={"Retry-After": "7"})], [])
        with self.assertRaises(FC.FR24RateLimited) as cm:
            c.flight_list("B-2091", "reg")
        self.assertEqual(cm.exception.retry_after, 7.0)
        self.assertEqual(cm.exception.http_status, 429)

    def test_401_relogin_once_then_ok(self):
        logins = []
        c = _client([_Resp(401), _Resp(200, self.OK)], logins)
        res = c.flight_list("CA1002")
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(logins, [True])
        self.assertEqual(c._lib_client.calls[1][1]["token"], "k1")   # new key used after re-login

    def test_403_twice_raises_auth(self):
        logins = []
        c = _client([_Resp(403), _Resp(403)], logins)
        with self.assertRaises(FC.FR24AuthError):
            c.flight_list("CA1002")
        self.assertEqual(logins, [True])

    def test_500_generic(self):
        c = _client([_Resp(500)], [])
        with self.assertRaises(FC.FR24Error) as cm:
            c.flight_list("CA1002")
        self.assertNotIsInstance(cm.exception, (FC.FR24AuthError, FC.FR24RateLimited))
        self.assertEqual(cm.exception.http_status, 500)

    def test_exception_message_has_no_token(self):
        c = _client([RuntimeError("boom https://x/flight/list.json?query=CA1&token=SECRET")], [])
        with self.assertRaises(FC.FR24Error) as cm:
            c.flight_list("CA1")
        self.assertNotIn("SECRET", str(cm.exception))


class SessionCacheTest(unittest.TestCase):
    def test_age_and_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text('{"saved_ts": %d, "subscriptionKey": "x"}' % int(time.time()), encoding="utf-8")
            self.assertIsNotNone(FC._load_session_cache(p))
            self.assertIsNone(FC._load_session_cache(p, now=time.time() + 7 * 3600))
            p.write_text('{"saved_ts": %d}' % int(time.time()), encoding="utf-8")
            self.assertIsNone(FC._load_session_cache(p))
            self.assertIsNone(FC._load_session_cache(Path(d) / "missing.json"))


if __name__ == "__main__":
    unittest.main()
