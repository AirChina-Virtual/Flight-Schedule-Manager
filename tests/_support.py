"""Shared test helpers: import paths, test data directory, cached config / plan.

Test data directory (``$VCCA_HOME`` for the whole test run, set here before any vcca module reads it):

* default: the fixture home ``tests/fixtures/home`` (small real sample, built by ``tools/make_fixtures.py``), so the
  suite runs in a clean clone / CI with no local data;
* ``VCCA_TEST_HOME=<dir>``: a real data directory (``vcca_plan.py test --full`` sets it to ``$VCCA_HOME``). Tests
  only READ it; tests that write use their own temporary directories.

Tests that only make sense on a full real season are decorated with ``@full_data`` (skipped unless
``VCCA_TEST_HOME`` is set). Expectations that depend on the fixture sample come from ``FIXTURE`` (fixture.json,
written by the generator).
"""

from __future__ import annotations

import functools
import json
import os
import sys
import unittest
from pathlib import Path

WORK_DIR = Path(__file__).resolve().parent.parent
if str(WORK_DIR) not in sys.path:
    sys.path.insert(0, str(WORK_DIR))

FIXTURE_HOME = WORK_DIR / "tests" / "fixtures" / "home"
TEST_HOME = os.environ.get("VCCA_TEST_HOME", "").strip()
FULL_DATA = bool(TEST_HOME)
HOME = Path(TEST_HOME).resolve() if FULL_DATA else FIXTURE_HOME
os.environ["VCCA_HOME"] = str(HOME)           # never the caller's $VCCA_HOME implicitly

SEASON = os.environ.get("VCCA_TEST_SEASON", "2026-27-1")
RULES_TEMPLATE = WORK_DIR / "rules"        # rules shipped with the code (copied to $VCCA_HOME/rules on deploy)
FIXTURE: dict = json.loads((FIXTURE_HOME / "fixture.json").read_text(encoding="utf-8"))

#: decorator for tests / classes that need the full real data (exact season counts, the whole-season run)
full_data = unittest.skipUnless(FULL_DATA, "full real data only (set VCCA_TEST_HOME, or run `vcca_plan.py test --full`)")
#: decorator for assertions about the fixture sample itself (fixture.json values)
fixture_only = unittest.skipIf(FULL_DATA, "fixture-sample expectations (not run against VCCA_TEST_HOME)")


@functools.lru_cache(maxsize=None)
def cfg():
    from vcca.config import load_config

    return load_config(SEASON)


@functools.lru_cache(maxsize=None)
def plan():
    from vcca import caac

    return caac.load_plan(cfg())


def season_dir() -> Path:
    return cfg().season_dir
