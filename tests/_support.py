"""Shared test helpers: import paths, cached config / plan."""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

WORK_DIR = Path(__file__).resolve().parent.parent
if str(WORK_DIR) not in sys.path:
    sys.path.insert(0, str(WORK_DIR))

SEASON = os.environ.get("VCCA_TEST_SEASON", "2026-27-1")


@functools.lru_cache(maxsize=None)
def cfg():
    from vcca.config import load_config

    return load_config(SEASON)


@functools.lru_cache(maxsize=None)
def plan():
    from vcca import caac

    return caac.load_plan(cfg())
