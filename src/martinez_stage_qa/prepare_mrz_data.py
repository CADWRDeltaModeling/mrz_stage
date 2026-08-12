from __future__ import annotations

import warnings

import pandas as pd

from dms_datastore.read_multi import read_ts_repo
from vtools.functions.unit_conversions import M2FT
from vtools import days

from . import paths
NAVD = 2.68
NOAA_START = pd.Timestamp("2013-01-01")

# Default per-source freshness tolerance (see martinez_workflow.md sec 7).
# Separately tunable per source; 1 day means a shorter fetch implies the NOAA
# API froze us out or the DWR repo hiccuped.
DEFAULT_TAU = pd.Timedelta(days=2)

# Canonical, un-year-stamped session-data paths (a rolling fetch must not be
# named after a fixed year). Resolved via paths.py so the layout is centralized.
DWR_REPO_CSV = paths.DWR_REPO
HARMONIC_CSV = paths.HARMONIC
NOAA_CSV = paths.NOAA
SF_CSV = paths.SF
MAL_CSV = paths.MAL


def _fail_or_warn(msg: str, *, hard: bool) -> None:
    if hard:
        raise ValueError(f"STALE SOURCE: {msg}")
    warnings.warn(f"STALE SOURCE (non-critical): {msg}", stacklevel=2)


def _check_freshness(
    name: str,
    df: pd.DataFrame,
    end: pd.Timestamp,
    tau: pd.Timedelta,
    *,
    hard: bool,
    trailing_nan_frac: float = 0.5,
    window_days: int = 7,
) -> None:
    """Fail (or warn) if a fetched source is stale relative to the requested end.

    Two failure modes: (1) trailing shortfall -- last valid sample is more than
    ``tau`` before ``end``; (2) trailing gap -- the final ``window_days`` are
    mostly NaN.
    """
    s = df["value"]
    valid = s.dropna()
    if valid.empty:
        _fail_or_warn(f"{name}: no valid data in fetched range", hard=hard)
        return
    gap = end - valid.index.max()
    if gap > tau:
        _fail_or_warn(
            f"{name}: last valid {valid.index.max():%Y-%m-%d %H:%M} is {gap} short "
            f"of requested end {end:%Y-%m-%d %H:%M} (tolerance {tau})",
            hard=hard,
        )
        return
    tail = s.loc[end - pd.Timedelta(days=window_days):end]
    if len(tail) and tail.isna().mean() > trailing_nan_frac:
        _fail_or_warn(
            f"{name}: trailing {window_days}d is {tail.isna().mean():.0%} NaN "
            f"(limit {trailing_nan_frac:.0%})",
            hard=hard,
        )


def _to_15min_value(s: pd.Series) -> pd.DataFrame:
    """Place a regular series on the canonical 15-min grid.

    ``read_ts_repo`` already returns a validated, regular series, so the native
    step is read straight off the index (no sort/validate). Sources finer than
    15 min (e.g. NOAA at 6 min) are linearly interpolated onto the 15-min marks;
    real gaps are preserved by not bridging more than a single native step.
    Sources at or coarser than 15 min are just placed on the grid (the old
    ``asfreq`` behavior).
    """
    grid = pd.date_range(s.index[0], s.index[-1], freq="15min")
    step = s.index[1] - s.index[0]
    if step < pd.Timedelta("15min"):
        limit = max(int(pd.Timedelta("15min") / step) - 1, 1)
        s = (
            s.reindex(s.index.union(grid))
            .interpolate(method="time", limit=limit, limit_area="inside")
            .reindex(grid)
        )
    else:
        s = s.reindex(grid)
    df = s.to_frame(name="value")
    df.index.name = "datetime"
    return df


def _read_whitespace_dt_value(fn: str) -> pd.Series:
    """Read a headerless whitespace file of ``date time value`` into a value series.

    Combines the first two columns (date, time) into a DatetimeIndex. Replaces
    the removed pandas ``parse_dates=[[0, 1]]`` column-combining form.
    """
    raw = pd.read_csv(fn, sep=r"\s+", header=None)
    idx = pd.to_datetime(raw[0].astype(str) + " " + raw[1].astype(str))
    return pd.Series(raw.iloc[:, 2].to_numpy(), index=idx, name="value")


def prepare(
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    *,
    dwr_tau: pd.Timedelta = DEFAULT_TAU,
    noaa_tau: pd.Timedelta = DEFAULT_TAU,
    neighbor_tau: pd.Timedelta = DEFAULT_TAU,
    trailing_nan_frac: float = 0.5,
) -> None:
    """Fetch sources and write the canonical 15-min CSVs consumed downstream.

    ``end`` is the moving cutoff (an ISO date or NOW resolved by the caller).
    Critical feeds (DWR repo, NOAA) hard-fail when staler than their tolerance;
    neighbor feeds (SF, MAL) only warn.
    """
    start = pd.Timestamp("1990-01-01") if start is None else pd.Timestamp(start)
    end = pd.Timestamp("2026-01-02") if end is None else pd.Timestamp(end)
    to_navd = pd.Timestamp(2006, 1, 1)

    # Spawn (or clear) the ephemeral session-data directory for this run.
    paths.ensure_session(clear=True)

    # ------------------------------------------------------------------
    # DWR repo Martinez stage (the series to be vetted/corrected post-2013)
    # ------------------------------------------------------------------
    mrz_repo = read_ts_repo("mrz", "elev", "upper", start="2000-01-01", end=end).squeeze()
    mrz_repo.index.name = "datetime"
    # keep the same NAVD adjustment logic used historically in mrz_data.py
    mrz_repo.loc[:to_navd] += NAVD
    mrz_repo.name = "value"
    df_dwr = _to_15min_value(mrz_repo)
    _check_freshness("DWR repo (mrz)", df_dwr, end, dwr_tau, hard=True,
                     trailing_nan_frac=trailing_nan_frac)
    df_dwr.to_csv(DWR_REPO_CSV, float_format="%.3f")

    mrz_ha = _read_whitespace_dt_value(str(paths.HARMONIC_SOURCE)).loc[start:(end + days(366))]
    mrz_ha.name = "value"
    _to_15min_value(mrz_ha).to_csv(HARMONIC_CSV, float_format="%.3f")

    mrz_noaa = (read_ts_repo("mrz2", "elev").squeeze() * M2FT).loc[NOAA_START:end]
    mrz_noaa.index.name = "datetime"
    mrz_noaa.name = "value"
    df_noaa = _to_15min_value(mrz_noaa)
    if df_noaa.index.min() < NOAA_START - pd.Timedelta(days=7):
        raise ValueError(f"NOAA MRZ begins {df_noaa.index.min()}, expected ~{NOAA_START}")
    _check_freshness("NOAA (mrz2)", df_noaa, end, noaa_tau, hard=True,
                     trailing_nan_frac=trailing_nan_frac)
    df_noaa.to_csv(NOAA_CSV, float_format="%.3f")

    sf = (read_ts_repo("sffpx", "elev").squeeze() * M2FT).loc[start:end]
    sf.index.name = "datetime"
    sf.name = "value"
    df_sf = _to_15min_value(sf)
    _check_freshness("SF (sffpx)", df_sf, end, neighbor_tau, hard=False,
                     trailing_nan_frac=trailing_nan_frac)
    df_sf.to_csv(SF_CSV, float_format="%.3f")

    mal = read_ts_repo("mal", "elev", "upper").squeeze()
    mal.loc[:to_navd] += NAVD
    mal = mal.loc[start:end]
    mal.index.name = "datetime"
    mal.name = "value"
    df_mal = _to_15min_value(mal)
    _check_freshness("MAL (mal)", df_mal, end, neighbor_tau, hard=False,
                     trailing_nan_frac=trailing_nan_frac)
    df_mal.to_csv(MAL_CSV, float_format="%.3f")


if __name__ == "__main__":
    prepare()