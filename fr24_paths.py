"""Shared directory paths for the 2026-2027 Winter-Spring FR24 audit.

Layout (fr24_work is a sibling of the season folder, not inside it):
    F:\\vCCA\\Data\\workspaces.db               <- REPO_ROOT/Data
    F:\\vCCA\\plan\\                             <- PLAN_ROOT
        fr24_work\\                            <- WORK_DIR (this file)
        2026-27-1\\                            <- SEASON_DIR (raw CAAC .xls + deliverables)
        outputs\\01a1e82b-...\\                 <- PREP_OUTPUT_DIR

Override the season folder with env var VCCA_SEASON (e.g. "2027-1") for a new season.
"""

import os

SEASON_NAME = os.environ.get("VCCA_SEASON", "2026-27-1")

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
PLAN_ROOT = os.path.dirname(WORK_DIR)
REPO_ROOT = os.path.dirname(PLAN_ROOT)
SEASON_DIR = os.path.join(PLAN_ROOT, SEASON_NAME)

PREP_OUTPUT_DIR = os.path.join(PLAN_ROOT, "outputs", "01a1e82b-ea24-72a1-9061-35a908c3c149")
WORKSPACES_DB = os.path.join(REPO_ROOT, "Data", "workspaces.db")

CACHE_DIR = os.path.join(WORK_DIR, "fr24_cache")
AP_CACHE_DIR = os.path.join(CACHE_DIR, "airports")
OUT_XLSX = os.path.join(SEASON_DIR, "2026-2027冬春航季_FlightRadarAPI核查明细.xlsx")
