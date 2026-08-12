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

import pandas as pd
import matplotlib.pyplot as plt
from vtools.functions.transition import transition_ts

from . import paths

# Transition window
TRANSITION_START = "2013-12-20"
TRANSITION_END = "2014-01-01"


def transition(show: bool = True, output=None):
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
    
    # Save output
    final_series.to_csv(out / paths.FINAL, header=True, float_format="%.3f")
    print(f"\nSaved final series to {out / paths.FINAL}")
    print(f"Final series: {final_series.index.min()} to {final_series.index.max()}")
    
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



if __name__ == "__main__":
    transition()
