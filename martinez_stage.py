#!/usr/bin/env python3
"""
QA/QC for Martinez tidal water level time series (DWR vs NOAA vs harmonic).

Inputs (CSV, in current working directory by default):
  - mrz_dwr_repo.csv   (DWR: column 'value', ~15-min)
  - mrz_noaa_martinez.csv   (NOAA: column 'value', 6-min)
  - mrz_harmonic.csv   (Harmonic: column 'value', ~15-min)

All are assumed to be readable via:
  pd.read_csv(fname, header=0, parse_dates=True, comment="#", index_col=0)

Outputs:
  - martinez_flags.csv: time-indexed diagnostics and combined bad mask (for DWR)
  - martinez_intervals.csv: merged "bad" intervals with reasons
  - martinez_qaqc.png: overview plots to review decisions

Assumptions:
  - Index is a DatetimeIndex (monotonicity not checked).
  - Values are water level in consistent units within each series.
  - vtools is available for cosine_lanczos filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from vtools.functions.filter import cosine_lanczos  # assume present; fail hard if missing
from vtools.functions.neighbor_fill import fill_from_neighbor
from vtools.data.gap import gap_count

import paths


# ----------------------------
# Configuration
# ----------------------------

DWR_FILE = str(paths.DWR_REPO)
NOAA_FILE = str(paths.NOAA)
HARM_FILE = str(paths.HARMONIC)

OUT_FLAGS = paths.FLAGS
OUT_INTERVALS = paths.INTERVALS
OUT_PLOT = paths.QAQC_PLOT
OUT_PLOT_ZOOM = paths.QAQC_ZOOM


@dataclass
class Params:
    # target uniform time step (DWR cadence)
    freq: str = "15min"
    # interpolate up to this many missing steps after resample

    interp_limit_filter: int = 4

    gap_bad_count = 4

    # subtidal filter
    subtidal_filter: str = "40h"          # vtools window spec
    noise_lowpass: str = "75min"

    # slow datum drift window
    offset_win: str = "45d"               # robust median over many days
    offset_smooth_win: str = "10d"        # extra smoothing on top

    # windowed lag vs harmonic (clock shift diagnostic)
    lag_win: str = "5d"                   # window length
    lag_step: str = "1d"                  # step between windows
    lag_max: pd.Timedelta = pd.Timedelta("2h")   # search +/- this much
    lag_flag: pd.Timedelta = pd.Timedelta("40min")  # flag if |lag| > this
    lag_persist_windows: int = 2          # require this many consecutive bad windows

    # harmonic residual "energy" diagnostic
    resid_band_win: str = "3d"            # window for IQR of residual band
    resid_ratio_thresh: float = 0.3       # DWR IQR / NOAA IQR ratio threshold
    resid_k_abs: float = 0.5              # DWR IQR above its own baseline

    # --- NEW: "noisy DWR" diagnostic using total variation of high-passed residual ---
    tv_win: str = "9h"                    # rolling window for total variation. This seems (provisionally) unneeded now that we have the noise_lowpass above.
    tv_ratio_thresh: float = 0.38          # TV(dwr)/TV(noaa) threshold
    tv_k_abs: float = 0.750                 # DWR TV above its own baseline

    # --- NEW: "too smooth / flat derivative" diagnostic ---
    # Uses IQR of first differences of the DWR tidal band vs NOAA tidal band.
    d1_win: str = "7h"
    d1_ratio_low: float = 0.42             # flag if IQR(Δdwr)/IQR(Δnoaa) < this
    d1_abs_frac: float = 0.5              # also require IQR(Δdwr) < d1_abs_frac * median(IQR(Δdwr))
 


    # subtidal disagreement diagnostic
    subdiff_win: str = "7d"
    subdiff_k: float = 1.0                # multiple of IQR for flagging
    subdiff_abs_thresh: float = 0.07       # NEW: absolute |subdiff| threshold

    # interval building / expansion
    merge_gap: pd.Timedelta = pd.Timedelta("12h")   # merge flag gaps <= this
    expand: pd.Timedelta = pd.Timedelta("3h")      # expand each bad interval by this on each side

    # recent-past zoom plot: days back from the series end
    zoom_days: int = 21


C_RESID = "C0"   # residual IQR ratio
C_TV    = "C1"   # total variation ratio
C_D1    = "C2"   # first-derivative ratio
C_SUBDIFF = "C3"  # subtidal difference
C_SHIFT = "C4"   # clock shift lag
# ----------------------------
# Utilities
# ----------------------------

def _read_series(path: str, value_col: str) -> pd.Series:
    """
    Read a CSV into a Series(time->value) using the user's preferred options:
      header=0, parse_dates=True, comment="#", index_col=0

    Raises on non-numeric data when casting to float.
    """
    df = pd.read_csv(path, header=0, parse_dates=True, comment="#", index_col=0)
    s = df[value_col].astype(float)
    # ensure DatetimeIndex
    if not isinstance(s.index, pd.DatetimeIndex):
        raise TypeError(f"{path}: index is not a DatetimeIndex after read_csv")
    return s


def _resample_and_interpolate(s: pd.Series, freq: str, interp_limit: int) -> pd.Series:
    """
    Resample to a uniform grid with given frequency, then interpolate
    up to interp_limit missing steps. Larger gaps remain NaN.
    """
    s2 = s.resample(freq).mean()
    if interp_limit is not None and interp_limit >= 0:
        s2 = s2.interpolate(limit=interp_limit)
    return s2


def _robust_rolling_iqr(x: pd.Series, win: str) -> pd.Series:
    q75 = x.rolling(win, center=True, min_periods=1).quantile(0.75)
    q25 = x.rolling(win, center=True, min_periods=1).quantile(0.25)
    return q75 - q25


def _rolling_median(x: pd.Series, win: str) -> pd.Series:
    return x.rolling(win, center=True, min_periods=1).median()


def window_samples(period: str, freq: str, odd: bool = True) -> int:
    """
    Convert a window period (e.g. '2h') to a number of samples
    given a series frequency (e.g. '15min').

    If odd=True, force the result to be odd.
    """
    win_td = pd.to_timedelta(period)
    freq_td = pd.to_timedelta(freq)

    n = int(round(win_td / freq_td))
    if n < 1:
        raise ValueError("Window too short for series frequency")

    if odd and n % 2 == 0:
        n += 1

    return n

def lowpass(x: pd.Series, win: str):
    periods = window_samples(win, x.index.freq, odd=True)
    return x.rolling(win, center=True, min_periods=3).mean()

def _merge_boolean_to_intervals(
    flag: pd.Series,
    gap_merge: pd.Timedelta,
) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """
    Convert boolean flag(time)->interval list, merging gaps shorter than gap_merge.
    """
    if flag.dtype != bool:
        flag = flag.astype(bool)

    idx = flag.index
    on = flag.to_numpy()

    intervals: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    i = 0
    n = len(on)
    while i < n:
        if not on[i]:
            i += 1
            continue
        start = idx[i]
        j = i
        while j + 1 < n and on[j + 1]:
            j += 1
        end = idx[j]
        intervals.append((start, end))
        i = j + 1

    if not intervals:
        return []

    # merge intervals whose gaps are <= gap_merge
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        ps, pe = merged[-1]
        if s - pe <= gap_merge:
            merged[-1] = (ps, e)
        else:
            merged.append((s, e))
    return merged


def _expand_intervals(
    intervals: List[Tuple[pd.Timestamp, pd.Timestamp]],
    expand: pd.Timedelta,
    tmin: pd.Timestamp,
    tmax: pd.Timestamp,
) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """
    Expand each interval by 'expand' on each side, clipped to [tmin, tmax],
    and re-merge any overlaps.
    """
    if not intervals:
        return []

    expanded = []
    for s, e in intervals:
        s2 = max(tmin, s - expand)
        e2 = min(tmax, e + expand)
        expanded.append((s2, e2))

    expanded.sort(key=lambda x: x[0])
    merged: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    for s, e in expanded:
        if not merged:
            merged.append((s, e))
        else:
            ps, pe = merged[-1]
            if s <= pe:
                merged[-1] = (ps, max(pe, e))
            else:
                merged.append((s, e))
    return merged


# ----------------------------
# Diagnostics
# ----------------------------

def compute_subtidal(z: pd.Series, window: str) -> pd.Series:
    """Subtidal estimate via cosine_Lanczos filter from vtools."""
    return cosine_lanczos(z.interpolate(limit=125), window)


def estimate_slow_offset(zsub_dwr: pd.Series, zsub_noaa: pd.Series, p: Params) -> pd.Series:
    """Slowly varying vertical offset based on subtidal difference."""
    d = zsub_dwr - zsub_noaa
    off = _rolling_median(d, p.offset_win)
    off = _rolling_median(off, p.offset_smooth_win)
    return off


def windowed_best_lag(
    x: pd.Series,
    y: pd.Series,
    win: pd.Timedelta,
    step: pd.Timedelta,
    max_lag: pd.Timedelta,
    freq: pd.Timedelta,
) -> pd.Series:
    """
    For each window center, compute lag that maximizes correlation corr(x(t), y(t+lag)).
    Returns a Series of Timedelta values indexed by window centers.
    """
    if len(x) != len(y) or not x.index.equals(y.index):
        raise ValueError("windowed_best_lag: x and y must be aligned on same index")

    idx = x.index

    t0 = idx[0] + win / 2
    t1 = idx[-1] - win / 2
    if t1 <= t0:
        raise ValueError("windowed_best_lag: time span too short for window")

    centers = pd.date_range(t0, t1, freq=step)
    max_lag_steps = int(np.round(max_lag / freq))
    lags = np.arange(-max_lag_steps, max_lag_steps + 1)

    out = []
    for c in centers:
        a = c - win / 2
        b = c + win / 2
        xs = x.loc[a:b].to_numpy()
        ys = y.loc[a:b].to_numpy()
        if len(xs) == 0 or len(ys) == 0:
            out.append(pd.NaT)
            continue

        # require some finite overlap
        if np.isfinite(xs).sum() < 0.7 * len(xs) or np.isfinite(ys).sum() < 0.7 * len(ys):
            out.append(pd.NaT)
            continue

        xs = xs - np.nanmedian(xs)
        ys = ys - np.nanmedian(ys)

        best_lag = None
        best_r = -np.inf

        for k in lags:
            if k < 0:
                xk = xs[-k:]
                yk = ys[: len(xs) + k]
            elif k > 0:
                xk = xs[:-k]
                yk = ys[k:]
            else:
                xk = xs
                yk = ys

            m = np.isfinite(xk) & np.isfinite(yk)
            if m.sum() < 0.7 * len(xk):
                continue

            xv = xk[m]
            yv = yk[m]
            den = np.sqrt(np.sum(xv * xv) * np.sum(yv * yv))
            if den <= 0:
                continue
            r = float(np.sum(xv * yv) / den)
            if r > best_r:
                best_r = r
                best_lag = k

        if best_lag is None:
            out.append(pd.NaT)
        else:
            out.append(best_lag * freq)

    return pd.Series(out, index=centers, name="best_lag")

def _robust_series_iqr(x: pd.Series) -> float:
    """IQR over time for a Series (robust scale), ignoring NaNs."""
    q75 = float(np.nanpercentile(x, 75))
    q25 = float(np.nanpercentile(x, 25))
    return max(q75 - q25, 1e-12)

def _rolling_total_variation(x: pd.Series, win: str) -> pd.Series:
    """Rolling total variation ~ sum(|Δx|) over a centered window."""
    return x.diff().abs().rolling(win, center=True, min_periods=1).sum()


# ----------------------------
# Main QA/QC workflow
# ----------------------------

def _window_ylim_masked(arrays, mask, margin: float = 0.05):
    """Y-limits covering finite values of `arrays` where boolean `mask` is True.

    Used to rescale the recent-past zoom so short-term detail is legible instead
    of being flattened by the full-record y-range.
    """
    lo, hi = np.inf, -np.inf
    for a in arrays:
        v = np.asarray(a, dtype=float)
        if v.shape[0] != mask.shape[0]:
            continue
        v = v[mask]
        v = v[np.isfinite(v)]
        if v.size:
            lo = min(lo, float(v.min()))
            hi = max(hi, float(v.max()))
    if np.isfinite(lo) and np.isfinite(hi) and hi > lo:
        pad = (hi - lo) * margin
        return lo - pad, hi + pad
    return None


def run(show: bool = True, output=None):
    p = Params()
    out = paths.output_dir(output)

    # 1) Load series
    z_dwr = _read_series(DWR_FILE, "value")
    z_noaa = _read_series(NOAA_FILE, "value")
    z_harm = _read_series(HARM_FILE, "value")

    # 2) Common overlapping time domain
    tmin = max(z_dwr.index.min(), z_noaa.index.min(), z_harm.index.min())
    tmax = min(z_dwr.index.max(), z_noaa.index.max(), z_harm.index.max())
    if tmax <= tmin:
        raise ValueError("No overlapping time range across DWR/NOAA/harmonic")

    z_dwr = z_dwr.loc[tmin:tmax]
    z_noaa = z_noaa.loc[tmin:tmax]
    z_harm = z_harm.loc[tmin:tmax]

    # 3) Resample to uniform 15-min grid and interpolate
    z_dwr_u = _resample_and_interpolate(z_dwr, p.freq, p.interp_limit_filter)
    z_dwr_u_raw = z_dwr_u.copy()
    z_noaa_u = _resample_and_interpolate(z_noaa, p.freq, p.interp_limit_filter)
    z_harm_u = _resample_and_interpolate(z_harm, p.freq, p.interp_limit_filter)

    res = z_dwr_u - z_harm_u
    r_lp = lowpass(res, p.noise_lowpass)
    z_dwr_u = r_lp + z_harm_u


    # Align exact index
    idx = z_dwr_u.index.intersection(z_noaa_u.index).intersection(z_harm_u.index)
    z_dwr_u = z_dwr_u.loc[idx]
    z_dwr_u_raw = z_dwr_u_raw.loc[idx]
    z_noaa_u = z_noaa_u.loc[idx]
    z_harm_u = z_harm_u.loc[idx]

    tmin_u, tmax_u = idx.min(), idx.max()
    freq_td = pd.to_timedelta(p.freq)

    gap_cnt = gap_count(z_dwr_u)   # Series
    gap_bad = gap_cnt > p.gap_bad_count


    # 4) Subtidal estimates and slow vertical offset
    zsub_dwr = compute_subtidal(z_dwr_u, p.subtidal_filter)
    zsub_noaa = compute_subtidal(z_noaa_u, p.subtidal_filter)
    offset = estimate_slow_offset(zsub_dwr, zsub_noaa, p)

    z_dwr_aligned = z_dwr_u - offset
    zsub_dwr_aligned = zsub_dwr - offset

    # 5) Tidal-band components (for lag check)
    ztid_dwr = z_dwr_aligned - zsub_dwr_aligned

    zsub_harm = compute_subtidal(z_harm_u, p.subtidal_filter)
    ztid_harm = z_harm_u - zsub_harm

    ztid_noaa = z_noaa_u - zsub_noaa

    # These are used for derivative tests, where the low-pass noise prodedure would hide issues
    z_dwr_raw_aligned = z_dwr_u_raw - offset
    zsub_dwr_raw = compute_subtidal(z_dwr_u_raw, p.subtidal_filter)
    zsub_dwr_raw_aligned = zsub_dwr_raw - offset
    ztid_dwr_raw = z_dwr_raw_aligned - zsub_dwr_raw_aligned


    # 6) Windowed lag vs harmonic (clock shift diagnostic)
    lag = windowed_best_lag(
        x=ztid_dwr,
        y=ztid_harm,
        win=pd.Timedelta(p.lag_win),
        step=pd.Timedelta(p.lag_step),
        max_lag=p.lag_max,
        freq=freq_td,
    )

    lag_flag = lag.abs() > p.lag_flag

    # Persistence: require K consecutive bad windows
    lag_persist = lag_flag.rolling(
        p.lag_persist_windows,
        min_periods=p.lag_persist_windows,
    ).sum() == p.lag_persist_windows

    # Map lag_persist from window centers back to full time grid (nearest neighbor)
    lag_persist_full = lag_persist.reindex(idx, method="nearest").fillna(False)

    # 7) Harmonic residual energy diagnostic (relative to NOAA)
    # DWR residual vs harmonic (after vertical alignment)
    r_dwr = z_dwr_aligned - z_harm_u

    # NOAA comparator: tidal-band proxy (no harmonic yet in this prototype)
    r_noaa = z_noaa_u - zsub_noaa

    # High-pass residuals (remove slow drift again)
    r_dwr_hp = r_dwr - compute_subtidal(r_dwr, p.subtidal_filter)
    r_noaa_hp = r_noaa - compute_subtidal(r_noaa, p.subtidal_filter)

    iqr_dwr = _robust_rolling_iqr(r_dwr_hp, p.resid_band_win)
    iqr_noaa = _robust_rolling_iqr(r_noaa_hp, p.resid_band_win)

    iqr_dwr_med = float(np.nanmedian(iqr_dwr))
    iqr_dwr_iqr = float(
        np.nanpercentile(iqr_dwr, 75) - np.nanpercentile(iqr_dwr, 25)
    )
    iqr_dwr_iqr = max(iqr_dwr_iqr, 1e-12)

    resid_abs_flag = iqr_dwr > (iqr_dwr_med + p.resid_k_abs * iqr_dwr_iqr)
    # define a floor for "meaningful" NOAA IQR
    iqr_noaa_floor = np.nanmedian(iqr_noaa[iqr_noaa > 0]) * 0.05 if np.any(iqr_noaa > 0) else 0.0

    resid_ratio = pd.Series(np.nan, index=idx)
    valid = (iqr_noaa > iqr_noaa_floor)
    resid_ratio[valid] = iqr_dwr[valid] / iqr_noaa[valid]

    # now the flag only where ratio is defined and large
    resid_ratio_flag = (resid_ratio > p.resid_ratio_thresh).fillna(False)

    resid_flag = (resid_abs_flag & resid_ratio_flag).fillna(False)



    # 7b) NEW: Total-variation (TV) diagnostic for "noisy DWR" periods
    tv_dwr = _rolling_total_variation(r_dwr_hp, p.tv_win)
    tv_noaa = _rolling_total_variation(r_noaa_hp, p.tv_win)

    tv_dwr_med = float(np.nanmedian(tv_dwr))
    tv_dwr_iqr = _robust_series_iqr(tv_dwr)
    tv_abs_flag = tv_dwr > (tv_dwr_med + p.tv_k_abs * tv_dwr_iqr)

    tv_noaa_floor = np.nanmedian(tv_noaa[tv_noaa > 0]) * 0.05 if np.any(tv_noaa > 0) else 0.0
    #tv_ratio = pd.Series(np.nan, index=idx)
    #tv_valid = (tv_noaa > tv_noaa_floor)
    #tv_ratio[tv_valid] = tv_dwr[tv_valid] / tv_noaa[tv_valid]
    #tv_ratio_flag = (tv_ratio > p.tv_ratio_thresh).fillna(False)
    #tv_flag = (tv_abs_flag & tv_ratio_flag).fillna(False)

    # --- DISABLE TV (provisionally) ---
    tv_dwr = pd.Series(np.nan, index=idx)
    tv_noaa = pd.Series(np.nan, index=idx)
    tv_ratio = pd.Series(np.nan, index=idx)
    tv_flag = pd.Series(False, index=idx, dtype=bool)



    # 7c) NEW: "too-flat first derivative" diagnostic (corner cutting / stuck sensor)
    # Compute IQR of first differences of the tidal band.
    d1_dwr = ztid_dwr_raw.diff()
    d1_noaa = ztid_noaa.diff()
    iqr_d1_dwr = _robust_rolling_iqr(d1_dwr, p.d1_win)
    iqr_d1_noaa = _robust_rolling_iqr(d1_noaa, p.d1_win)

    d1_noaa_floor = np.nanmedian(iqr_d1_noaa[iqr_d1_noaa > 0]) * 0.05 if np.any(iqr_d1_noaa > 0) else 0.0
    d1_ratio = pd.Series(np.nan, index=idx)
    d1_valid = (iqr_d1_noaa > d1_noaa_floor)
    d1_ratio[d1_valid] = iqr_d1_dwr[d1_valid] / iqr_d1_noaa[d1_valid]
    d1_ratio_low_flag = (d1_ratio < p.d1_ratio_low).fillna(False)

    iqr_d1_dwr_med = float(np.nanmedian(iqr_d1_dwr))
    d1_abs_flat_flag = (iqr_d1_dwr < p.d1_abs_frac * iqr_d1_dwr_med).fillna(False)

    d1_flat_flag = (d1_ratio_low_flag & d1_abs_flat_flag).fillna(False)


    # 8) Subtidal disagreement (after offset correction)
    subdiff = zsub_dwr_aligned - zsub_noaa

    # Simple absolute threshold; ignore IQR for now
    subdiff_flag = subdiff.abs() > p.subdiff_abs_thresh
    subdiff_flag = subdiff_flag.fillna(False)


    # 9) Combined "bad DWR" flag
    # The TV flag is currently disabled above. | tv_flag 
    combined = (lag_persist_full | resid_flag | d1_flat_flag | subdiff_flag | gap_bad).astype(bool)
 
    # 9a) Build a *fill/action mask* from expanded intervals
    #
    # The pointwise boolean flags (e.g., tv_flag) can "stutter" True/False because they are
    # computed from rolling-window metrics. For filling/replacement we want contiguous episodes.
    #
    # Convert combined -> merged intervals -> expanded intervals -> boolean mask.

    comb_ints_fill = _merge_boolean_to_intervals(combined, p.merge_gap)
    comb_ints_fill = _expand_intervals(comb_ints_fill, p.expand, tmin_u, tmax_u)

    combined_fill = pd.Series(False, index=idx, dtype=bool)
    for s, e in comb_ints_fill:
        # idx is a DateTimeIndex; slice assignment marks all timestamps in [s, e]
        combined_fill.loc[s:e] = True

    # 9b) Masked and neighbor-filled DWR series
    #
    # NOTE ON "ALIGNED" SPACE:
    # The slow 'offset' is a *relative drift* estimate (DWR subtidal - NOAA subtidal)
    # For filling, we remove this slow relative drift so the DWR↔NOAA relationship is 
    # approximately stationary, fill in that drift-free (aligned) space, then add the 
    # drift term back so the reconstructed series
    # remains in the DWR frame.

    # Mask in the original DWR frame (useful as a diagnostic output)
    dwr_masked = z_dwr_u.copy()
    dwr_masked[combined_fill] = np.nan

    # Mask in the drift-free aligned frame (this is what we actually fill)
    dwr_aligned_masked = z_dwr_aligned.copy()
    dwr_aligned_masked[combined_fill] = np.nan

    # Fill the aligned series from NOAA using residual-interpolation against NOAA
    # (aligned_DWR ~ a + b * NOAA on good data, then interpolate residuals across gaps)
    nf_res = fill_from_neighbor(
        target=dwr_aligned_masked,
        neighbor=z_noaa_u,
        method="resid_interp_pchip",
        # you can add kwargs here later (e.g., max_gap=...) if you want to limit extrapolation
    )
    dwr_aligned_filled = nf_res["filled"]

    # Log the neighbor-fill regression provenance (baseline DWR_aligned ~ a + b*NOAA).
    # These are the one-glance health stats referenced in the workflow Reviewer's guide.
    _info = nf_res.get("model_info", {}) or {}
    _base = _info.get("baseline", {}) or {}
    a_fit = float(_base.get("a", np.nan))
    b_fit = float(_base.get("b", np.nan))
    sigma_fit = float(_info.get("sigma_resid", np.nan))
    n_over = int(_info.get("n_overlap", 0))
    _m = dwr_aligned_masked.notna() & z_noaa_u.notna()
    if _m.sum() > 2 and np.isfinite(a_fit) and np.isfinite(b_fit):
        _y = dwr_aligned_masked[_m].to_numpy(float)
        _x = z_noaa_u[_m].to_numpy(float)
        _yhat = a_fit + b_fit * _x
        _ss_res = float(np.nansum((_y - _yhat) ** 2))
        _ss_tot = float(np.nansum((_y - np.nanmean(_y)) ** 2))
        r2_fit = 1.0 - _ss_res / _ss_tot if _ss_tot > 0 else np.nan
        last_overlap = dwr_aligned_masked[_m].index.max()
    else:
        r2_fit = np.nan
        last_overlap = None
    print(
        "NOAA neighbor-fill regression (DWR_aligned ~ a + b*NOAA):\n"
        f"  b (tidal amplification) = {b_fit:.4f}\n"
        f"  a (intercept, ft)       = {a_fit:.4f}\n"
        f"  R^2                     = {r2_fit:.5f}\n"
        f"  sigma_resid (ft)        = {sigma_fit:.4f}\n"
        f"  overlap points          = {n_over}\n"
        f"  overlap through         = {last_overlap}"
    )
    if np.isfinite(r2_fit) and (r2_fit < 0.95 or abs(b_fit - 1.0) > 0.05):
        print("  [check] regression looks off (R^2 low or b far from ~1.01) "
              "-- inspect DWR/NOAA agreement before trusting the fill.")

    # Return to the DWR frame by re-applying the slow relative drift term
    dwr_corrected = dwr_aligned_filled + offset



    # Build intervals per reason

    intervals = []

    for reason, f in [
        ("clock_shift", lag_persist_full),
        ("resid_spike", resid_flag),
        ("tv_noisy", tv_flag),
        ("d1_flat", d1_flat_flag),        
        ("subtidal_disagree", subdiff_flag),
    ]:
        ints = _merge_boolean_to_intervals(f, p.merge_gap)
        ints = _expand_intervals(ints, p.expand, tmin_u, tmax_u)
        for s, e in ints:
            intervals.append({"start": s, "end": e, "reason": reason})

    # Combined intervals
    comb_ints = _merge_boolean_to_intervals(combined, p.merge_gap)
    comb_ints = _expand_intervals(comb_ints, p.expand, tmin_u, tmax_u)
    for s, e in comb_ints:
        intervals.append({"start": s, "end": e, "reason": "combined"})

    # Subtidal-only intervals (for optional separate shading)
    subdiff_ints = _merge_boolean_to_intervals(subdiff_flag, p.merge_gap)
    subdiff_ints = _expand_intervals(subdiff_ints, p.expand, tmin_u, tmax_u)


    # 10) Write flag time series
    # Simple csv with "final" corrected series
    dwr_corrected.name = "mrz_elev_corrected"
    dwr_corrected.to_csv(out / paths.CORRECTED_2013, index_label="datetime", float_format="%.3f",header=True,date_format="%Y-%m-%dT%H:%M")

    # Detailed flags
    flags = pd.DataFrame(
        {   
            "mrz_elev_raw": z_dwr_u_raw,
            "mrz_elev": z_dwr_u,
            "mrz_noaa": z_noaa_u,
            "mrz_ha": z_harm_u,
            "offset_slow": offset,
            "zsub_dwr_aligned": zsub_dwr_aligned,
            "zsub_noaa": zsub_noaa,
            "subdiff": subdiff,
            "lag_window_center": lag.reindex(idx, method="nearest"),
            "clock_shift_flag": lag_persist_full,
            "resid_iqr_dwr": iqr_dwr,
            "resid_iqr_noaa": iqr_noaa,
            "resid_ratio": resid_ratio,
            "resid_flag": resid_flag,
            "tv_dwr": tv_dwr,
            "tv_noaa": tv_noaa,
            "tv_ratio": tv_ratio,
            "tv_flag": tv_flag,
            "d1_ratio": d1_ratio,
            "d1_flat_flag": d1_flat_flag,
            "subtidal_flag": subdiff_flag,
            "gap_count": gap_cnt.reindex(idx),
            "gap_bad": gap_bad,
            "bad_dwr": combined,
            "bad_dwr_fill": combined_fill,  # expanded for filling
            "mrz_elev_masked": dwr_masked,
            "mrz_elev_corrected": dwr_corrected,
            # Canonical alias for downstream stitching:
            # use 'value' as the corrected MRZ series, without removing legacy columns.
            "value_masked": dwr_masked,
            "value": dwr_corrected,            
        },
        index=idx,
    )
    flags.to_csv(out / OUT_FLAGS, index_label="time",float_format="%.3f")

    # 11) Write intervals
    intervals_df = pd.DataFrame(intervals).sort_values(["start", "reason"])
    intervals_df.to_csv(out / OUT_INTERVALS, index=False)

    # 12) Plots
    print("Generating QA/QC plot...")
    fig = plt.figure(figsize=(14, 10))
    gs = fig.add_gridspec(4, 1, height_ratios=[2.0, 1.0, 1.0, 1.0], hspace=0.25)

    ax0 = fig.add_subplot(gs[0])
    # DWR provenance (pre-noise-filter) + working (noise-filtered) + masked/corrected
    ax0.plot(
        idx, z_dwr_u_raw,
        label="DWR mrz_elev (raw, resampled)",
        linewidth=0.6, alpha=0.35
    )
    ax0.plot(
        idx, z_dwr_u,
        label=f"DWR mrz_elev (noise-filtered > {p.noise_lowpass})",
        linewidth=0.8, alpha=0.9
    )
    ax0.plot(idx, dwr_masked, label="DWR mrz_elev (masked)", linewidth=0.8)
    ax0.plot(idx, dwr_corrected, label="DWR mrz_elev (corrected)", linewidth=1.4)
    ax0.plot(idx, zsub_dwr, label="DWR subtidal", linestyle="--", alpha=0.7)

    # Reference series
    ax0.plot(idx, z_noaa_u, label="NOAA mrz_noaa", alpha=0.8)
    ax0.plot(idx, z_harm_u, label="Harmonic mrz_ha", alpha=0.8)

    # Shading for intervals (unchanged logic)
    # Diagnostic-matched shading (color-consistent with ax2)
    for s, e in _merge_boolean_to_intervals(lag_persist_full, p.merge_gap):
        ax0.axvspan(s, e, color=C_SHIFT, alpha=0.10)
    
    for s, e in _merge_boolean_to_intervals(resid_flag, p.merge_gap):
        ax0.axvspan(s, e, color=C_RESID, alpha=0.10)

    for s, e in _merge_boolean_to_intervals(tv_flag, p.merge_gap):
        ax0.axvspan(s, e, color=C_TV, alpha=0.10)

    for s, e in _merge_boolean_to_intervals(d1_flat_flag, p.merge_gap):
        ax0.axvspan(s, e, color=C_D1, alpha=0.10)

    for s, e in _merge_boolean_to_intervals(subdiff_flag, p.merge_gap):
        ax0.axvspan(s, e, color=C_SUBDIFF, alpha=0.15)  # or define C_SUBDIFF

    ax0.set_ylabel("Water level")

    ax0.plot([], [], color=C_SHIFT, label="Clock shift")
    ax0.plot([], [], color=C_RESID, label="Resid anomaly")
    #ax0.plot([], [], color=C_TV,    label="Noisy (TV)")     # Disabled
    ax0.plot([], [], color=C_D1,    label="Too smooth")
    ax0.plot([], [], color=C_SUBDIFF, label="Subtidal disagree")

    # Lift legend into unused whitespace above the axes (reduce crowding)
    ax0.legend(
        loc="upper left",
        bbox_to_anchor=(0.0, 1.18),
        fontsize="small",
        ncol=2,
        frameon=True,
    )

    ax1 = fig.add_subplot(gs[1], sharex=ax0)
    # lag in minutes
    lag_min = lag / np.timedelta64(1, "m")
    ax1.plot(lag_min.index, lag_min)
    ax1.axhline(p.lag_flag / np.timedelta64(1, "m"), linestyle="--")
    ax1.axhline(-p.lag_flag / np.timedelta64(1, "m"), linestyle="--")
    ax1.set_ylabel("Best lag (min)")

    ax2 = fig.add_subplot(gs[2], sharex=ax0)

    # --- Residual IQR ratio ---
    ax2.plot(
        idx, resid_ratio,
        color=C_RESID,
        label="Residual IQR ratio (DWR / NOAA)",
    )
    ax2.axhline(
        p.resid_ratio_thresh,
        color=C_RESID,
        linestyle="--",
        label=f"Resid ratio thresh = {p.resid_ratio_thresh}",
    )
    ax2.fill_between(
        idx,
        p.resid_ratio_thresh,
        resid_ratio,
        where=(resid_ratio > p.resid_ratio_thresh),
        color=C_RESID,
        alpha=0.15,
        label="_nolegend_",
    )

    # --- Total variation ratio ---    # Disabled
    if False:   
        ax2.plot(
            idx, tv_ratio,
            color=C_TV,
            alpha=0.9,
            label="Total variation ratio (DWR / NOAA)",
        )
        ax2.axhline(
            p.tv_ratio_thresh,
            color=C_TV,
            linestyle="--",
            label=f"TV ratio thresh = {p.tv_ratio_thresh}",
        )
        ax2.fill_between(
            idx,
            p.tv_ratio_thresh,
            tv_ratio,
            where=(tv_ratio > p.tv_ratio_thresh),
            color=C_TV,
            alpha=0.15,
            label="_nolegend_",
        )

    # --- First-derivative (flatness) ratio ---
    ax2.plot(
        idx, d1_ratio,
        color=C_D1,
        alpha=0.9,
        label="d/dt IQR ratio (DWR / NOAA)",
    )
    ax2.axhline(
        p.d1_ratio_low,
        color=C_D1,
        linestyle="--",
        label=f"d/dt low thresh = {p.d1_ratio_low}",
    )
    ax2.fill_between(
        idx,
        d1_ratio,
        p.d1_ratio_low,
        where=(d1_ratio < p.d1_ratio_low),
        color=C_D1,
        alpha=0.15,
        label="_nolegend_",
    )

    ax2.set_ylabel("Relative variability ratios")
    ax2.legend(loc="upper right", fontsize="small", ncol=1)
    ax2.set_ylim(0.0, 2.)


    ax3 = fig.add_subplot(gs[3], sharex=ax0)
    ax3.axhline(p.subdiff_abs_thresh, linestyle="--",color = C_SUBDIFF)
    ax3.axhline(-p.subdiff_abs_thresh, linestyle="--",color = C_SUBDIFF)
    ax3.plot(idx, subdiff, color=C_SUBDIFF)
    ax3.set_ylabel("Subtidal diff\n(DWR_aligned - NOAA)")
    ax3.set_xlabel("Time")
    ax3.set_ylim(-0.3, 0.3)

    fig.subplots_adjust
    fig.suptitle("Martinez QA/QC diagnostics (shaded = combined bad intervals)")
    fig.savefig(out / OUT_PLOT, dpi=200)

    # Recent-past zoom: last `zoom_days` ending at the series end, with the
    # autoscaled y-axes (ax0 level, ax1 lag) rescaled to the visible window.
    x1 = tmax_u
    x0 = x1 - pd.Timedelta(days=p.zoom_days)
    ax0.set_xlim(x0, x1)
    mask0 = (idx >= x0) & (idx <= x1)
    yl0 = _window_ylim_masked(
        [z_dwr_u_raw, z_dwr_u, dwr_masked, dwr_corrected, zsub_dwr, z_noaa_u, z_harm_u],
        mask0,
    )
    if yl0:
        ax0.set_ylim(*yl0)
    lmask = (lag_min.index >= x0) & (lag_min.index <= x1)
    yl1 = _window_ylim_masked([lag_min.to_numpy()], lmask)
    if yl1:
        ax1.set_ylim(*yl1)
    fig.suptitle(f"Martinez QA/QC diagnostics - last {p.zoom_days} days (ending {x1:%Y-%m-%d})")
    fig.savefig(out / OUT_PLOT_ZOOM, dpi=200)

    if show:
        plt.show()
    plt.close(fig)
    print("Done.")


if __name__ == "__main__":
    run()
