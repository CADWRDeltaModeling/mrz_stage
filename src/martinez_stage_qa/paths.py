"""Centralized filesystem layout for the Martinez stage pipeline.

Three tiers keep frozen inputs, per-run scratch, and deliverables separate so
the pipeline can eventually be deployed (ship ``data/`` + ``output/``, never the
ephemeral ``session_data/``):

``data/``
    Frozen, deployable, version-controlled artifacts: the harmonic source, the
    hand-cleaned legacy record, the DFM parameters, and the frozen pre-2014
    legacy fill. Never cleared automatically; the legacy fill is only rewritten
    on an explicit ``--rebuild-legacy`` / ``legacy`` run. It lives *inside* the
    ``martinez_stage_qa`` package itself (``[tool.setuptools.package-data]`` in
    ``pyproject.toml``), so it comes with the install and is found the same
    way regardless of the current working directory.

``session_data/``
    Ephemeral per-run sliced fetches (DWR repo, NOAA, harmonic, SF, MAL).
    Cleared and respawned at the start of every ``prepare`` run. Excluded from
    deployment.

``output/``
    Final and review products. Defaults to ``./output`` but callers may point it
    at any location (an "assumed" vs. "indicated" output directory).

``ROOT`` is the project/deployment directory (containing ``session_data/`` and
``output/``): it defaults to the current working directory and can be
overridden with the ``MARTINEZ_STAGE_ROOT`` environment variable.
"""
from __future__ import annotations

import os
import shutil
from importlib import resources
from pathlib import Path

ROOT = Path(os.environ.get("MARTINEZ_STAGE_ROOT", Path.cwd())).resolve()

# Bundled with the package (see [tool.setuptools.package-data]); overridable
# via MARTINEZ_STAGE_DATA_DIR for pointing at a different copy during testing.
_PACKAGE_DATA_DIR = Path(str(resources.files("martinez_stage_qa") / "data"))
DATA_DIR = Path(os.environ.get("MARTINEZ_STAGE_DATA_DIR", _PACKAGE_DATA_DIR)).resolve()

SESSION_DIR = ROOT / "session_data"
DEFAULT_OUTPUT_DIR = ROOT / "output"

# ---- data/ (frozen, deployable) ------------------------------------------
HARMONIC_SOURCE = DATA_DIR / "mrzastro_1920_2035.csv"
CLEANED_LEGACY = DATA_DIR / "dms_mrz_cleaned_1990_2017.csv"
DFM_PARAMS = DATA_DIR / "dfm_trimbur_rw_mrz_sfsub.yaml"
LEGACY_FILL = DATA_DIR / "mrz_stage_filled_legacy.csv"

# ---- session_data/ (ephemeral, per-run slices) ---------------------------
DWR_REPO = SESSION_DIR / "mrz_dwr_repo.csv"
HARMONIC = SESSION_DIR / "mrz_harmonic.csv"
NOAA = SESSION_DIR / "mrz_noaa_martinez.csv"
SF = SESSION_DIR / "sf_stage.csv"
MAL = SESSION_DIR / "mal_stage.csv"

# ---- output/ (products; basenames only, directory chosen at runtime) -----
CORRECTED_2013 = "dms_mrz_elev_2013_9999.csv"
FLAGS = "martinez_flags.csv"
INTERVALS = "martinez_intervals.csv"
QAQC_PLOT = "martinez_qaqc.png"
QAQC_ZOOM = "martinez_qaqc_zoom.png"
FINAL = "dms_mrz_elev_filled.csv"
TRANSITION_PLOT = "transition_martinez.png"


def output_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve (and create) the output directory.

    ``override`` selects an indicated location; ``None`` uses ``./output``.
    """
    d = Path(override) if override else DEFAULT_OUTPUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_session(clear: bool = True) -> Path:
    """Spawn (or clear + respawn) the ephemeral ``session_data/`` directory."""
    if clear and SESSION_DIR.exists():
        shutil.rmtree(SESSION_DIR)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR


def ensure_data() -> Path:
    """Ensure the frozen ``data/`` directory exists (does not clear it)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
