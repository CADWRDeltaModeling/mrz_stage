#!/usr/bin/env python3
"""Rolling CLI to advance the continuous Martinez (mrz) stage series.

Orchestrates, in-process::

    prepare  ->  qaqc  ->  transition

so a single ``--end`` (an ISO date or ``NOW``) threads through the whole
pipeline. The legacy (pre-NOAA) fill is frozen and is *not* re-run by ``run``
unless ``--rebuild-legacy`` is given.

Examples
--------
    update_martinez_stage run --end 2026-08-08
    update_martinez_stage run --end NOW
    update_martinez_stage legacy          # cold-start seed (rare)
"""
from __future__ import annotations

import logging
from pathlib import Path

import click
import pandas as pd

from dms_datastore.logging_config import configure_logging, resolve_loglevel

from . import prepare_mrz_data
from . import martinez_stage
from . import transition_martinez_stage
from . import mrz_legacy_fill
from . import paths

LEGACY_START = pd.Timestamp("1991-02-01")

logger = logging.getLogger(__name__)


def _logging_options(f):
    """Attach the standard --logdir/--loglevel/--debug/--quiet options.

    Mirrors the dms_datastore CLI logging convention.
    """
    f = click.option(
        "--logdir", type=click.Path(file_okay=False, path_type=Path),
        default="logs", show_default=True,
        help="Directory for timestamped run logfiles.",
    )(f)
    f = click.option(
        "--loglevel", default=None,
        help="Explicit level (DEBUG/INFO/WARNING/...); overrides --debug/--quiet.",
    )(f)
    f = click.option(
        "--debug", is_flag=True, default=False, help="Log at DEBUG level.",
    )(f)
    f = click.option(
        "--quiet", is_flag=True, default=False,
        help="Silence console logging (the file log is still written).",
    )(f)
    return f


def _configure_logging(logdir, debug, quiet, loglevel, *, prefix):
    """Resolve flags and configure the martinez_stage_qa logger tree."""
    level, console = resolve_loglevel(debug=debug, quiet=quiet, loglevel=loglevel)
    logpath = configure_logging(
        package_name="martinez_stage_qa",
        level=level,
        console=console,
        logdir=logdir,
        logfile_prefix=prefix,
    )
    if logpath is not None:
        logger.info("logging to %s", logpath)
    return logpath


class TimestampParam(click.ParamType):
    """A click parameter accepting an ISO date, optionally the token ``NOW``."""

    name = "timestamp"

    def __init__(self, allow_now: bool = False):
        self.allow_now = allow_now

    def convert(self, value, param, ctx):
        if value is None or isinstance(value, pd.Timestamp):
            return value
        text = str(value).strip()
        if self.allow_now and text.upper() == "NOW":
            return pd.Timestamp.now().floor("15min")
        try:
            return pd.Timestamp(text)
        except (ValueError, TypeError):
            self.fail(
                f"{value!r} is not a valid date"
                + (" or 'NOW'" if self.allow_now else ""),
                param,
                ctx,
            )


END = TimestampParam(allow_now=True)
START = TimestampParam(allow_now=False)


def _freshness_options(f):
    """Attach the separately-tunable per-source freshness options."""
    f = click.option(
        "--dwr-tau-days", type=float, default=2.0, show_default=True,
        help="Max staleness (days) for the DWR repo feed before a hard failure.",
    )(f)
    f = click.option(
        "--noaa-tau-days", type=float, default=2.0, show_default=True,
        help="Max staleness (days) for the NOAA feed before a hard failure.",
    )(f)
    f = click.option(
        "--neighbor-tau-days", type=float, default=2.0, show_default=True,
        help="Max staleness (days) for the SF/MAL neighbor feeds (warn only).",
    )(f)
    f = click.option(
        "--trailing-nan-frac", type=float, default=0.5, show_default=True,
        help="Max fraction of NaN allowed in the trailing 7 days of a feed.",
    )(f)
    return f


def _run_prepare(start, end, dwr_tau_days, noaa_tau_days, neighbor_tau_days, trailing_nan_frac):
    prepare_mrz_data.prepare(
        start=start,
        end=end,
        dwr_tau=pd.Timedelta(days=dwr_tau_days),
        noaa_tau=pd.Timedelta(days=noaa_tau_days),
        neighbor_tau=pd.Timedelta(days=neighbor_tau_days),
        trailing_nan_frac=trailing_nan_frac,
    )


@click.group()
def update_martinez_stage():
    """Advance the continuous Martinez (mrz) filled stage series."""


@update_martinez_stage.command()
@click.option("--start", type=START, default=None, help="Start date (default 1991-02-01).")
@click.option("--end", type=END, default="NOW", show_default=True,
              help="End cutoff: an ISO date or NOW.")
@click.option("--rebuild-legacy", is_flag=True, default=False,
              help="Also regenerate the frozen pre-NOAA legacy fill (needed on a cold start).")
@click.option("--output", type=click.Path(file_okay=False), default=None,
              help="Output directory for products (default ./output).")
@click.option("--plot-orig-data", is_flag=True, default=False,
              help="Save a raw-data (mrz/NOAA) inspection plot around each corrected-series NaN span.")
@_freshness_options
@_logging_options
def run(start, end, rebuild_legacy, output, plot_orig_data, dwr_tau_days, noaa_tau_days, neighbor_tau_days, trailing_nan_frac, logdir, loglevel, debug, quiet):
    """Run the full pipeline: prepare -> qaqc -> transition."""
    _configure_logging(logdir, debug, quiet, loglevel, prefix="update_run")
    logger.info("pipeline start: end=%s rebuild_legacy=%s output=%s", end, rebuild_legacy, output)
    click.echo(f"[1/3] prepare  (end={end:%Y-%m-%d %H:%M})")
    _run_prepare(start, end, dwr_tau_days, noaa_tau_days, neighbor_tau_days, trailing_nan_frac)
    if rebuild_legacy:
        click.echo("[*]   legacy fill (frozen pre-NOAA)")
        mrz_legacy_fill.fill_mrz_legacy(start=LEGACY_START, end=mrz_legacy_fill.NOAA_START, show=False)
    click.echo("[2/3] qaqc")
    martinez_stage.run(show=False, output=output, plot_orig=plot_orig_data)
    click.echo("[3/3] transition")
    transition_martinez_stage.transition(show=False, output=output)
    click.echo(f"done -> {paths.output_dir(output) / paths.FINAL}")


@update_martinez_stage.command()
@click.option("--start", type=START, default=None, help="Start date (default 1991-02-01).")
@click.option("--end", type=END, default="NOW", show_default=True,
              help="End cutoff: an ISO date or NOW.")
@_freshness_options
@_logging_options
def prepare(start, end, dwr_tau_days, noaa_tau_days, neighbor_tau_days, trailing_nan_frac, logdir, loglevel, debug, quiet):
    """Fetch sources and write the canonical 15-min CSVs (with freshness guard)."""
    _configure_logging(logdir, debug, quiet, loglevel, prefix="prepare")
    _run_prepare(start, end, dwr_tau_days, noaa_tau_days, neighbor_tau_days, trailing_nan_frac)


@update_martinez_stage.command()
@click.option("--output", type=click.Path(file_okay=False), default=None,
              help="Output directory for products (default ./output).")
@click.option("--plot-orig-data", is_flag=True, default=False,
              help="Save a raw-data (mrz/NOAA) inspection plot around each corrected-series NaN span.")
@_logging_options
def qaqc(output, plot_orig_data, logdir, loglevel, debug, quiet):
    """Run post-NOAA QA/QC + correction (auto-derives its own time window)."""
    _configure_logging(logdir, debug, quiet, loglevel, prefix="qaqc")
    martinez_stage.run(show=False, output=output, plot_orig=plot_orig_data)


@update_martinez_stage.command()
@click.option("--output", type=click.Path(file_okay=False), default=None,
              help="Output directory for products (default ./output).")
@_logging_options
def transition(output, logdir, loglevel, debug, quiet):
    """Blend the legacy and corrected series into the final product."""
    _configure_logging(logdir, debug, quiet, loglevel, prefix="transition")
    transition_martinez_stage.transition(show=False, output=output)


@update_martinez_stage.command()
@click.option("--start", type=START, default=None, help="Start date (default 1991-02-01).")
@click.option("--end", type=END, default=None,
              help="Legacy end (defaults to the frozen NOAA_START).")
def legacy(start, end):
    """Regenerate the frozen pre-NOAA legacy fill (rarely needed)."""
    mrz_legacy_fill.fill_mrz_legacy(
        start=LEGACY_START if start is None else start,
        end=mrz_legacy_fill.NOAA_START if end is None else end,
        show=False,
    )


if __name__ == "__main__":
    update_martinez_stage()
