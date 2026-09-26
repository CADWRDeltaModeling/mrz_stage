#!/usr/bin/env python3
"""
Transition between mrz_data.py filled output and martinez_stage.py corrected output.

Reads:
  - mrz_stage_filled_legacy.csv (from mrz_legacy_fill.py)
  - martinez_flags.csv (from martinez_stage.py)

Uses vtools transition_ts with blend method to smoothly transition from the
historical filled series to the NOAA-corrected series between Dec 20, 2013
and Jan 1, 2014.

Outputs:
  - dms_mrz_elev_filled.csv: Combined time series
  - transition_martinez.png: Visualization of the transition
"""

import logging

import pandas as pd
import matplotlib.pyplot as plt
from vtools.functions.transition import transition_ts

from . import paths
from . import qa_checks
from .martinez_stage import Params

logger = logging.getLogger(__name__)

# Transition window
TRANSITION_START = "2013-12-20"
TRANSITION_END = "2014-01-01"

# martinez_stage.estimate_slow_offset uses centered rolling windows
# (offset_win, then offset_smooth_win on top) to estimate the slow DWR/NOAA
# datum offset. A corrected series that starts too close to TRANSITION_START
# gives that estimate an asymmetric (mostly one-sided) window right where the
# legacy/NOAA splice happens, biasing it away from what a full-history run
# would produce there -- i.e. reaching the window is not enough on its own,
# the offset estimate also needs to be "warmed up" before it.
_p = Params()
_REQUIRED_MARGIN = pd.Timedelta(_p.offset_win) + pd.Timedelta(_p.offset_smooth_win)


def transition(show: bool = True, output=None, publish_start=None):
    """Blend the legacy and corrected series, write the final product.

    Parameters
    ----------
    publish_start : str or pandas.Timestamp, optional
        If given, the *written* product is sliced to ``>= publish_start``.
        This never affects the blend computation itself (always full history,
        so the centered rolling windows in `martinez_stage` stay warmed up) --
        it only trims what gets written to `paths.FINAL`, so the staged output
        for a given run can be a small, single, internally-consistent tail
        slice without reintroducing an edge effect at the slice boundary.
    """
    out = paths.output_dir(output)

    # Read the frozen legacy fill (pre-NOAA) from data/
    mrz_filled = pd.read_csv(
        paths.LEGACY_FILL,
        header=0,
        parse_dates=True,
        index_col=0,
        comment="#"
    )["value"]

    # Read the corrected series from the QA/QC flags product in output/
    martinez_corrected = pd.read_csv(
        out / paths.FLAGS,
        header=0,
        parse_dates=True,
        index_col=0,
        comment="#"
    )["mrz_elev_corrected"]
    martinez_corrected = martinez_corrected.resample('15min').asfreq()

    required_start = pd.Timestamp(TRANSITION_START) - _REQUIRED_MARGIN
    if martinez_corrected.index.min() > required_start:
        raise ValueError(
            f"transition needs the corrected series ({out / paths.FLAGS}, column "
            f"'mrz_elev_corrected') to reach back to at least {required_start.date()} "
            f"-- {_REQUIRED_MARGIN} before the fixed {TRANSITION_START} legacy/NOAA "
            f"splice window, so martinez_stage's centered rolling offset estimate "
            f"(offset_win={_p.offset_win} + offset_smooth_win={_p.offset_smooth_win}) "
            f"is warmed up by the time it reaches the splice, not lopsided by an "
            f"artificially short history. It only starts at "
            f"{martinez_corrected.index.min()}. This happens when `qaqc`/`run` was "
            f"given `--start` too close to (or after) {TRANSITION_START} -- that flag "
            f"bounds how far back qaqc reprocesses. Re-run `qaqc` (or `run`) with "
            f"`--start` <= {required_start.date()}, or omit `--start` to use the "
            f"full-history default, before calling `transition`."
        )

    logger.info("MRZ filled (pre-NOAA): %s to %s", mrz_filled.index.min(), mrz_filled.index.max())
    logger.info("Martinez corrected (post-NOAA): %s to %s",
                martinez_corrected.index.min(), martinez_corrected.index.max())
    print(f"MRZ filled (pre-NOAA): {mrz_filled.index.min()} to {mrz_filled.index.max()}")
    print(f"Martinez filled (post-NOAA): {martinez_corrected.index.min()} to {martinez_corrected.index.max()}")
    print(f"Names: {mrz_filled.name}, {martinez_corrected.name}")
    # Use transition_ts with blend method
    # ts0 = mrz_filled (earlier/historical data)
    # ts1 = martinez_corrected (later/NOAA-corrected data)
    print(f"\nBlending from {TRANSITION_START} to {TRANSITION_END}...")
    
    final_series = transition_ts(
        mrz_filled,
        martinez_corrected,
        method="blend",
        window=(TRANSITION_START, TRANSITION_END),
        return_type="series",
        names="value"
    )
    final_series = final_series.resample('15min').asfreq()
    
    final_series.name = "value"
    final_series.index.name = "datetime"

    # publish_start only trims the written artifact; plots/checks below still
    # use the full final_series for complete splice-window context.
    series_to_write = (
        final_series if publish_start is None
        else final_series.loc[pd.Timestamp(publish_start):]
    )

    # Save output
    series_to_write.to_csv(out / paths.FINAL, header=True, float_format="%.3f")
    logger.info("saved final series to %s (%s -> %s)",
                out / paths.FINAL, series_to_write.index.min(), series_to_write.index.max())
    print(f"\nSaved final series to {out / paths.FINAL}")
    print(f"Final series: {series_to_write.index.min()} to {series_to_write.index.max()}")
    
    # Create visualization
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8))
    
    # Top panel: Full time series
    ax1.plot(mrz_filled.index, mrz_filled.values, 
             label="MRZ filled (mrz_data.py)", alpha=0.7, linewidth=0.8)
    ax1.plot(martinez_corrected.index, martinez_corrected.values, 
             label="Martinez corrected (with NOAA)", alpha=0.7, linewidth=0.8)
    ax1.plot(final_series.index, final_series.values, 
             label="Final blended", linewidth=1.2, color='black')
    
    # Shade transition window
    ax1.axvspan(pd.Timestamp(TRANSITION_START), pd.Timestamp(TRANSITION_END), 
                alpha=0.2, color='yellow', label='Blend window')
    
    ax1.set_ylabel("Water level (ft)")
    ax1.set_title("Martinez Stage: Transition from Historical to NOAA-Corrected Series")
    ax1.legend(loc='upper right')
    ax1.grid(True, alpha=0.3)
    
    # Bottom panel: Zoom in on transition window
    zoom_start = pd.Timestamp(TRANSITION_START) - pd.Timedelta(days=15)
    zoom_end = pd.Timestamp(TRANSITION_END) + pd.Timedelta(days=15)
    
    ax2.plot(mrz_filled.loc[zoom_start:zoom_end].index, 
             mrz_filled.loc[zoom_start:zoom_end].values,
             label="MRZ filled", alpha=0.7, linewidth=1.2)
    ax2.plot(martinez_corrected.loc[zoom_start:zoom_end].index, 
             martinez_corrected.loc[zoom_start:zoom_end].values,
             label="Martinez corrected", alpha=0.7, linewidth=1.2)
    ax2.plot(final_series.loc[zoom_start:zoom_end].index, 
             final_series.loc[zoom_start:zoom_end].values,
             label="Final blended", linewidth=1.5, color='black')
    
    ax2.axvspan(pd.Timestamp(TRANSITION_START), pd.Timestamp(TRANSITION_END), 
                alpha=0.2, color='yellow', label='Blend window')
    
    ax2.set_ylabel("Water level (ft)")
    ax2.set_xlabel("Date")
    ax2.set_title("Transition Window Detail")
    ax2.legend(loc='upper right')
    ax2.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(out / paths.TRANSITION_PLOT, dpi=200)
    print(f"Saved plot to {out / paths.TRANSITION_PLOT}")
    if show:
        plt.show()

    # Final-product gap check. The delivered series is the model input and must
    # be gap-free; fail loudly (after products + plot are written, so the
    # artifacts remain available for tracing) if any NaNs slipped through.
    qa_checks.report_nan_intervals(
        series_to_write,
        f"final product ({paths.FINAL})",
        raise_on_nan=True,
    )



if __name__ == "__main__":
    transition()
