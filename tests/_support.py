"""Shared test helpers: import paths, cached config / plan / legacy results."""

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


@functools.lru_cache(maxsize=None)
def legacy_rebuild():
    """The ORIGINAL fr24_work/rebuild_vamsys_plan.py module (kept in place; also copied to legacy/)."""
    import rebuild_vamsys_plan  # noqa: PLC0415

    return rebuild_vamsys_plan


@functools.lru_cache(maxsize=None)
def legacy_plan():
    return legacy_rebuild().load_raw_preplan()
