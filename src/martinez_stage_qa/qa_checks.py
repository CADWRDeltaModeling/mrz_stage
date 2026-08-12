"""Gap (NaN) checks for the Martinez stage products.

The whole point of the pipeline is to hand a downstream model a *gap-free*
15-min stage series, so any NaN in the delivered product is an error
condition -- not a cosmetic issue. These helpers

  * locate contiguous NaN runs in a time-indexed series
    (:func:`nan_intervals`),
  * print a pass/fail report and optionally raise
    (:func:`report_nan_intervals`), and
  * for the post-2013 corrected segment, attribute each NaN run to a probable
    cause from the pipeline inputs (:func:`diagnose_corrected_nans`) so a
    reviewer can tell *why* a span was dropped -- e.g. DWR present but the slow
    offset was undefined, a NOAA neighbor gap, or QA-masked DWR that NOAA could
    not fill.
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def nan_intervals(s: pd.Series) -> pd.DataFrame:
    """Return the contiguous runs of NaN in a time-indexed series.

    Columns: ``start``, ``end`` (inclusive timestamps of the run), ``n_missing``
    (samples in the run), and ``duration`` (``end - start``). Empty frame when
    the series has no NaNs.
    """
    isnan = s.isna().to_numpy()
    idx = s.index
    rows = []
    n = len(isnan)
    i = 0
    while i < n:
        if not isnan[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and isnan[j + 1]:
            j += 1
        start, end = idx[i], idx[j]
        rows.append(
            {
                "start": start,
                "end": end,
                "n_missing": j - i + 1,
                "duration": end - start,
            }
        )
        i = j + 1
    return pd.DataFrame(rows, columns=["start", "end", "n_missing", "duration"])


def report_nan_intervals(
    s: pd.Series,
    name: str,
    *,
    raise_on_nan: bool = False,
    max_list: int = 50,
) -> pd.DataFrame:
    """Print a pass/fail NaN report for ``s`` and return the NaN intervals.

    On a clean series prints a one-line ``OK``. Otherwise lists each NaN period
    and, when ``raise_on_nan`` is set, raises :class:`ValueError` (after the full
    listing is printed) so the pipeline fails fast on a gappy model input.
    """
    ivals = nan_intervals(s)
    total = int(s.isna().sum())
    if ivals.empty:
        logger.info(
            "[nan-check] %s: OK -- no NaNs across %d samples (%s -> %s).",
            name, len(s), s.index.min(), s.index.max(),
        )
        return ivals

    logger.warning(
        "[nan-check] %s: FOUND %d NaN samples in %d period(s):",
        name, total, len(ivals),
    )
    for _, r in ivals.head(max_list).iterrows():
        logger.warning(
            "    %s -> %s  (%d samples, %s)",
            r["start"], r["end"], r["n_missing"], r["duration"],
        )
    if len(ivals) > max_list:
        logger.warning("    ... %d more period(s)", len(ivals) - max_list)

    if raise_on_nan:
        raise ValueError(
            f"{name} contains {total} NaN sample(s) in {len(ivals)} period(s); "
            "the delivered product must be gap-free. See the [nan-check] "
            "listing above for the offending spans."
        )
    return ivals


def diagnose_corrected_nans(
    corrected: pd.Series,
    *,
    dwr: pd.Series,
    noaa: pd.Series,
    offset: pd.Series,
    mask: pd.Series,
) -> pd.DataFrame:
    """Attribute each NaN run in the corrected series to a probable cause.

    All inputs must share ``corrected``'s index. For every NaN run the fraction
    of samples where each input is available is summarized, and a single
    ``cause`` is inferred:

    * ``slow offset undefined`` -- ``dwr_corrected = filled + offset``; a NaN
      offset (no DWR/NOAA subtidal overlap over the 45 d window) drops the point
      even when raw DWR is present.
    * ``QA-masked DWR, no NOAA to fill`` -- DWR was masked by a QA flag and the
      NOAA neighbor is also absent, so the fill has nothing to draw on.
    * ``NOAA gap prevents neighbor fill`` / ``both DWR and NOAA missing`` /
      ``inputs present -- inspect fill`` cover the remaining cases.

    Returns the :func:`nan_intervals` frame with added ``dwr_present``,
    ``noaa_present``, ``offset_present``, ``qa_masked`` (fractions) and
    ``cause`` columns.
    """
    ivals = nan_intervals(corrected)
    if ivals.empty:
        return ivals

    mask = mask.astype(bool)
    dwr_present, noaa_present, offset_present, qa_masked, causes = [], [], [], [], []
    for _, r in ivals.iterrows():
        sl = slice(r["start"], r["end"])
        dwr_f = float(dwr.loc[sl].notna().mean())
        noaa_f = float(noaa.loc[sl].notna().mean())
        off_f = float(offset.loc[sl].notna().mean())
        mask_f = float(mask.loc[sl].mean())

        if off_f < 0.999:
            cause = "slow offset undefined (no DWR/NOAA subtidal overlap)"
        elif noaa_f < 0.999 and dwr_f < 0.5:
            cause = "both DWR and NOAA missing"
        elif noaa_f < 0.999 and mask_f > 0.5:
            cause = "QA-masked DWR, no NOAA to fill"
        elif noaa_f < 0.999:
            cause = "NOAA gap prevents neighbor fill"
        else:
            cause = "inputs present -- inspect fill_from_neighbor"

        dwr_present.append(dwr_f)
        noaa_present.append(noaa_f)
        offset_present.append(off_f)
        qa_masked.append(mask_f)
        causes.append(cause)

    ivals = ivals.copy()
    ivals["dwr_present"] = dwr_present
    ivals["noaa_present"] = noaa_present
    ivals["offset_present"] = offset_present
    ivals["qa_masked"] = qa_masked
    ivals["cause"] = causes
    return ivals
