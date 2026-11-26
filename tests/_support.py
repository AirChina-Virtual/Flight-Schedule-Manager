"""Shared test helpers: import paths, cached config / plan.

Tests that need real season data read it from the data directory ``$VCCA_HOME`` (a development copy laid out like
the server, see README.md), season ``$VCCA_TEST_SEASON`` (default 2026-27-1). Web/config tests build their own
temporary data directories and never write to ``$VCCA_HOME``.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

WORK_DIR = Path(__file__).resolve().parent.parent
if str(WORK_DIR) not in sys.path:
    sys.path.insert(0, str(WORK_DIR))

SEASON = os.environ.get("VCCA_TEST_SEASON", "2026-27-1")
RULES_TEMPLATE = WORK_DIR / "rules"        # rules shipped with the code (copied to $VCCA_HOME/rules on deploy)


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
