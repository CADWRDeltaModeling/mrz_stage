# Martinez Stage Filling — Workflow Documentation

Continuous, gap-filled Martinez (`mrz`) water-level series, 1990 → present, built by
splicing a **legacy harmonic-based fill** (pre-NOAA) onto a **NOAA-corrected QA/QC
series** (post-2013).

### Orientation — the mental model

Three ideas make the rest easy to read:

1. **DWR Martinez is the product; NOAA is the crutch.** The series we ship *is* the DWR
   gauge. NOAA Martinez–Amorco (~1 km away, effectively co-located) is a near-twin used to
   repair DWR — not to replace it.
2. **Two knobs do the correction.** A slowly varying vertical offset $\delta$ (month-scale
   datum difference, up to ~0.6 ft) between the two stations is removed before filling and added back after; and a
   single regression $\tilde z_{\text{DWR}} \approx a + b\,z_{\text{NOAA}}$ (tidal
   amplification $b \approx 1.01$) supplies tide inside gaps, with a smooth interpolated
   residual for the small leftover.
3. **Fixed 15-min grid, 1990 → present**, spliced from the frozen legacy fill (pre-2014) onto
   the NOAA-corrected series (post-2013) across a fixed Dec 2013 → Jan 2014 window.

NOAA actually pulls **triple duty** — it fills gaps, stabilizes drift, *and* is the comparator
for several of the QA "tripwires"; the harmonic is the tidal reference for the clock-shift and
residual-energy checks. The full referenyce-role map and a review checklist are in the
[Reviewer's guide](#8-reviewers-guide).

## 0. What to run (operator quick start)

This project is now an installable package (`martinez-stage-qa`, src-layout under
`src/martinez_stage_qa/`). Install it once (editable for development)—this also pulls
in the dependencies and wires up the console command:

```bash
pip install -e .
```

Data products (`output/*.csv`, `data/*.csv`, etc.) are tracked with **DVC** against an
S3-compatible MinIO remote (see `.dvc/config`). `pip install -e .` does not install DVC
itself — install it separately, including the S3 backend, then pull the tracked files:

```bash
conda install -c conda-forge dvc dvc-s3   # or: pip install "dvc[s3]"
dvc pull
```

The single entry point is the **`update_martinez_stage`** console command
(defined by `[project.scripts]` in `pyproject.toml`, backed by
`martinez_stage_qa.update_martinez_stage`) — a Click CLI that orchestrates
`prepare → qaqc → transition` in-process, threading one `--end` (an ISO date or
`NOW`) through the whole pipeline. Use it first, then use the
[Reviewer's guide](#8-reviewers-guide) to assess quality.

### 0.1 Update run (normal operation; advances post-2013 to "now")

```bash
update_martinez_stage run --end NOW
```

- `--end` accepts `NOW` or an ISO date (e.g. `--end 2026-08-08`); default is `NOW`.
- The frozen pre-NOAA legacy fill is **not** re-run unless `--rebuild-legacy` is given.
- Freshness guards are tunable per source: `--dwr-tau-days`, `--noaa-tau-days`,
  `--neighbor-tau-days`, `--trailing-nan-frac`.

Products updated by this run:

- `dms_mrz_elev_2013_9999.csv` (post-2013 corrected segment)
- `dms_mrz_elev_filled.csv` (final 1990→present stitched product)
- `martinez_flags.csv`, `martinez_intervals.csv`, `martinez_qaqc.png`,
  `martinez_qaqc_zoom.png`, `transition_martinez.png`
- with `--plot-orig-data`: `martinez_naninspect_*.png` per remaining-NaN span and
  `martinez_backupfill_*.png` per MAL/SF/harmonic backup-filled span
  ([§2.2](#22-backup-reconstruction-when-noaa-also-fails))

### 0.2 Full rebuild (only when legacy needs regeneration)

Use this only when bootstrapping from scratch or intentionally regenerating the
frozen legacy pre-2014 segment (`mrz_stage_filled_legacy.csv`):

```bash
update_martinez_stage run --end NOW --rebuild-legacy
```

To regenerate only the legacy seed (rare):

```bash
update_martinez_stage legacy
```

### 0.3 Individual stages (advanced / debugging)

The orchestrator also exposes each stage as its own subcommand:

```bash
update_martinez_stage prepare --end NOW
update_martinez_stage qaqc
update_martinez_stage transition
```

These are equivalent to invoking the underlying scripts directly
(`prepare_mrz_data.py` → `martinez_stage.py` → `transition_martinez_stage.py`),
but the subcommands share the same `--end` / freshness handling as `run`.

## 1. High-level pipeline

```mermaid
flowchart TD
    subgraph CURRENT["Current workflow (re-run to advance)"]
        P[prepare_mrz_data.py<br/>fetch repo → canonical CSVs] --> M[martinez_stage.py<br/>QA/QC + NOAA correction]
        M --> T[transition_martinez_stage.py<br/>blend legacy + corrected]
    end
    subgraph LEGACY["Legacy preparation (frozen pre-2014)"]
        L[mrz_legacy_fill.py<br/>harmonic-based fill 1990–2014]
    end
    P --> L
    L --> T
    T --> OUT[dms_mrz_elev_filled.csv<br/>final 1990 → present]
```

The three producers write into a common set of canonical CSVs (single `value` column,
regular 15-min grid). Everything downstream assumes that grid and does **not** resample.

## 2. Drift-free ("aligned") frame

The post-NOAA correction never fills the DWR series in its raw datum. Instead it removes
a **slowly varying relative offset** between DWR and NOAA, fills in that drift-free frame,
then restores the offset. This keeps the DWR↔NOAA relationship approximately stationary
during filling so that a regression/interpolation against NOAA is not biased by slow datum
creep (sensor re-referencing, biofouling, benchmark drift).

**Definitions.** Let $z_{\text{DWR}}$ and $z_{\text{NOAA}}$ be the 15-min series. The
subtidal component uses a 40-hour cosine-Lanczos filter $\mathcal{L}_{40\text{h}}$:

$$
z^{\text{sub}} = \mathcal{L}_{40\text{h}}[z].
$$

The relative offset is a **robust, doubly-smoothed** median of the subtidal difference:

$$
\delta(t) = \operatorname{med}_{10\text{d}}\!\Big(\operatorname{med}_{45\text{d}}\big[\,z^{\text{sub}}_{\text{DWR}} - z^{\text{sub}}_{\text{NOAA}}\,\big]\Big).
$$

- The **45-day** rolling median (`offset_win`) sets the *breadth* of the long-term drift
  estimate — wide enough to ignore spring/neap and weather-band energy and isolate
  month-scale datum drift.
- The **10-day** rolling median (`offset_smooth_win`) removes residual steppiness so
  $\delta$ is smooth where it is added back.

**Fill in the aligned frame, then restore:**

$$
\tilde{z}_{\text{DWR}} = z_{\text{DWR}} - \delta,
\qquad
z^{\text{corr}} = \operatorname{Fill}_{\text{NOAA}}\!\big[\tilde{z}_{\text{DWR}}\big] + \delta .
$$

Filling uses `fill_from_neighbor(..., method="resid_interp_pchip")`: fit a baseline
$\tilde{z}_{\text{DWR}} \approx a + b\,z_{\text{NOAA}}$ on good overlap, then PCHIP-interpolate
the residuals across masked gaps. Because $\delta$ is added back, the reconstruction stays
in the DWR frame.

**What counts as a "gap".** The spans that get filled are of two distinct kinds:

1. **Genuinely missing reported data** — telemetry holes in the DWR feed (beyond the short
   4-step interpolation limit used for tiny gaps).
2. **Data removed by anomaly detection** — the QA/QC intervals (clock shift, residual-IQR
   spike, flat derivative, subtidal disagreement) that are *masked to NaN before filling*.

Both are reconstructed the same way from NOAA, so a "filled" sample may reflect either a
*missing* reading or a *rejected* one. The QA-removed subset is recorded in the
`bad_dwr_fill` column of `martinez_flags.csv` (and shaded in the QA/QC plot), so a reviewer can
always tell which gaps were holes vs. which were deliberately cut.

> Tunable breadth: the 45 d / 10 d pair is the single most important "how long is long-term"
> choice. Narrower windows track drift more aggressively (and risk absorbing real signal);
> wider windows are more conservative.

> **Limitation — NOAA-outage offset dropout.** Because $\delta$ is a 45-day rolling median of
> the DWR−NOAA subtidal difference, a NOAA (mrz2) outage longer than that window leaves
> $\delta$ undefined in the interior of the hole. Since the product is
> $z^{\text{corr}} = (z_{\text{DWR}} - \delta) + \delta$, an undefined $\delta$ would drop
> *present* DWR. Where DWR is present (within the 4-step interpolation limit) and unmasked,
> $\delta$ cancels and the code passes the DWR series through unchanged, recorded in the
> `passthrough_noaa_gap` column of `martinez_flags.csv`. If DWR is **also** missing (beyond the
> 4-step limit) or QA-masked during a NOAA outage, there is nothing left for the NOAA-based fill
> to draw on; see [§2.2](#22-backup-reconstruction-when-noaa-also-fails) for how that case is
> now handled.

### 2.1 What the neighbor fill does in each frequency band

MRZ (DWR) and NOAA Martinez–Amorco are ~1 km apart in the same channel — effectively
co-located — so the single regression $\tilde z_{\text{DWR}} \approx a + b\,z_{\text{NOAA}}$
does most of the work, and it behaves cleanly in **both** bands.

- **Tidal band — supplied directly by NOAA.** Inside a gap the oscillation is
  $b\,z_{\text{NOAA}}(t)$: the observed NOAA tide scaled by the amplification $b$. As reported
  by the QA/QC run log (see [§8.5](#85-one-independent-check--read-it-from-the-run-log)),
  $b \approx 1.02$, $a \approx -0.07$ ft, $R^2 \approx 0.998$ ($\sim\!4\times10^{5}$ overlap
  points); an independent tidal-band std ratio gives $\approx 1.015$. So Martinez runs ~1–2%
  *higher* tidal amplitude than Amorco — small, but **not** unity, and the fit captures it.
  There is **no phase-lag term** (the regression is contemporaneous), so any MRZ↔NOAA phase
  difference lands in the residual; because PCHIP only smooths that residual across a gap, the
  reconstructed tide follows **NOAA's phase**. Fine for short gaps; a persistent fixed lag
  would be a small systematic tidal-phase error.

- **Subtidal band — small once δ is removed.** After the slow offset δ is subtracted, the
  residual subtidal difference is tiny: IQR $\approx 0.02$ ft and 95th-percentile
  $|\Delta| \approx 0.06$ ft — right at the `subdiff_abs_thresh` $= 0.07$ ft flag. δ itself
  carries the real, month-scale datum difference (range $\approx -0.23 \ldots +0.64$ ft). So
  the residual PCHIP must interpolate across a gap is small and slowly varying — the reason
  this one-shot full-resolution fill works where the legacy path needed an explicit subtidal
  model. (The larger *std* of the aligned difference, $\approx 0.14$ ft, is heavy-tailed from
  transient bad-sensor excursions — the anomalies QA masks, not real disagreement.)

**Edge behavior.** The 40 h subtidal filter NaNs the last $\approx 4$ days (its half-width),
but the corrected product still reaches the requested end: δ is a `min_periods=1` centered
rolling median, so it extends to the tip using the most recent valid subtidal difference, and
the fill rides on full-resolution DWR. Treat the trailing ~4 days as **provisional** — the
drift term is effectively *held* rather than freshly estimated, and subtidal-dependent QA
(subtidal-disagreement, clock-shift lag) is blind there. This is a soft limit, not a hard
truncation, and is far smaller than NOAA repo staleness (tens of days) as a practical bound on
how recent `--end` can be.

> These regression stats ($a$, $b$, $R^2$, $\sigma_{\text{resid}}$, overlap count) are computed
> inside `fill_from_neighbor` (returned in `model_info`) and **printed to the run log** by the
> QA/QC step — see the [Reviewer's guide §8.5](#85-one-independent-check--read-it-from-the-run-log).

### 2.2 Backup reconstruction when NOAA also fails

The NOAA-based fill ([§2](#2-drift-free-aligned-frame)) needs *some* signal to fill from — a
DWR/NOAA outage overlap (both unavailable over the same span) leaves it with nothing to draw
on. This does happen: a ~6-week `mrz2` (NOAA) outage in 2014 overlapped an ~18-day DWR
telemetry gap, and the corrected series would otherwise fail the final nan-check there.

For exactly this case, `martinez_stage.run` falls back to the **same reconstruction technique**
used for the frozen pre-2014 legacy fill ([§1](#1-high-level-pipeline), [§3](#3-the-trimbur--dynamic-factor-model-dfm-smoother)) —
factored out as `neighbor_style_fill` in `mrz_legacy_fill.py` and reused by both paths:

1. Subtidal fill by **MAL substitution** (San Joaquin at Mallard Island, the same-channel
    upstream neighbor), then **DFM smoothing** against San Francisco
    (`dfm_trimbur_rw_mrz_sfsub.yaml`, [§3](#3-the-trimbur--dynamic-factor-model-dfm-smoother)).
2. Tidal-band residual filled by interpolating against the **harmonic** residual
    (`resid_interp_linear`).

This backup only runs when the NOAA-based `dwr_corrected` still has NaNs left after the normal
fill and pass-through steps, and only over those remaining spans — it never overrides a good
NOAA-based reconstruction. It is spliced in with vtools'
[`ts_blend`](https://github.com/CADWRDeltaModeling/vtools3/blob/master/vtools/functions/blend.py)
(`Params.backup_blend_win`, default `"1d"`), a priority-ordered blend that overlays the backup
only where the NOAA-based series is missing, with a soft linear-weight taper over the blend
window so there is no step at the edges (the same reason `ts_blend`, not `transition_ts`, was
chosen here — it tolerates patching an arbitrary interior gap without a fully-specified
NaN-free window on both sides).

Samples rescued this way are recorded in the **`backup_filled`** column of
`martinez_flags.csv`, and (with `--plot-orig-data`) a detail plot is written per contiguous
span — `martinez_backupfill_<start>.png` — showing the raw DWR/NOAA inputs, the harmonic, the
MAL/SF/harmonic reconstruction, and the final blended output side by side
([§8](#8-reviewers-guide)). If a DWR+NOAA overlap gap is ever **not** covered by this backup
(e.g. MAL/SF/harmonic are themselves unavailable there too), the point is left NaN and still
fails the final nan-check — a real gap we want to surface, not invent.

## 3. The Trimbur / dynamic factor model (DFM) smoother

The legacy subtidal fill is polished with a **bivariate dynamic factor model** (`DFMFill`
in `vtools.functions.neighbor_fill`, a `statsmodels` state-space `MLEModel`). This will be
unfamiliar to most users, so a short primer:

A DFM assumes two observed series — here the target subtidal (Martinez) and a neighbor
subtidal (San Francisco) — are driven by a **shared latent factor** plus series-specific
anomalies and measurement noise. The shared factor is a *local-linear trend* (a smoothly
evolving level $\mu_t$ with slope $\beta_t$):

$$
\mu_{t+1} = \mu_t + \beta_t + \eta^\mu_t, \qquad
\beta_{t+1} = \beta_t + \zeta_t,\;\; \zeta_t \sim \mathcal{N}(0, q_\beta).
$$

$$
\underbrace{y_t}_{\text{Martinez}} = \mu_t + a^y_t + \varepsilon^y_t, \qquad
\underbrace{x_t}_{\text{SF}} = \lambda\,\mu_t + a^x_t + \varepsilon^x_t .
$$

The **Trimbur** variant fixes the level-shock variance to zero ($\operatorname{Var}(\eta^\mu)=0$),
so all roughness enters through the slope $\beta$. This yields an *integrated random walk* —
a smooth, twice-integrated trend that behaves like a cubic-spline-in-time rather than a jagged
random walk. The loading $\lambda$ lets SF and Martinez have different amplitudes; the
anomaly terms $a^y, a^x$ (random-walk `_rw` or AR(1) `_ar`) absorb station-specific behavior
so the neighbor does not contaminate the target.

In practice the model is fit **once** by maximum likelihood (Kalman filter + smoother), and
the estimated parameters are packed into `dfm_trimbur_rw_mrz_sfsub.yaml`. Subsequent runs
pass `params=` to **skip fitting** and just run the smoother — fast and reproducible. In the
legacy script the fit is gated by `do_fit=False`; the current legacy module always loads the
saved YAML.

## 4. Script categorization

```mermaid
flowchart LR
    subgraph LG["① Legacy preparation (frozen)"]
        l1[mrz_legacy_fill.py]
    end
    subgraph C["② Current workflow — update_martinez_stage.py orchestrator"]
        c1[prepare_mrz_data.py]
        c2[martinez_stage.py]
        c3[transition_martinez_stage.py]
        c1 --> c2 --> c3
    end
```

### ① Legacy preparation — frozen, correct, do **not** re-run to advance

- **`mrz_legacy_fill.py`** produces `mrz_stage_filled_legacy.csv`, covering
  1990 → `NOAA_START = 2014-05-10`. Its domain is historical and fixed; advancing the
  present does not change it. Inputs: `dms_mrz_cleaned_1990_2017.csv`, harmonic, SF, MAL,
  and `dfm_trimbur_rw_mrz_sfsub.yaml`. Its core computation is factored out as
  `neighbor_style_fill`, which `martinez_stage.py` also calls as the
  [backup reconstruction](#22-backup-reconstruction-when-noaa-also-fails) for post-2013
  DWR+NOAA overlap gaps.

> **Gotcha — the product's start date lives in the frozen file, not in `--start`.**
> `transition` (and therefore the final `dms_mrz_elev_filled.csv`) reads the legacy
> segment *directly from the on-disk `data/mrz_stage_filled_legacy.csv`*. The
> `--start` option and the `LEGACY_START` constant in `update_martinez_stage.py` only
> feed the **regeneration** paths (`legacy` and `run --rebuild-legacy`). A normal
> `prepare`/`run` treats the legacy fill as frozen and never rewrites it, so editing
> `LEGACY_START` (or passing `--start`) has **no effect** on the final series until you
> actually regenerate the frozen file. If the final nan-check reports a leading gap
> (e.g. `1990-01-01 → 1991-01-26`), it is coming from the frozen CSV's leading NaNs, not
> from the current run. To move the product's start, regenerate the legacy fill:
>
> ```bash
> update_martinez_stage legacy            # rebuild just the frozen seed
> # or: update_martinez_stage run --end NOW --rebuild-legacy
> ```
>
> Note also that the `if __name__ == "__main__"` block at the bottom of
> `mrz_legacy_fill.py` hardcodes its own `start` (`1990-02-01`), which differs from
> `LEGACY_START` (`1991-02-01`). Regenerate via the `legacy` CLI command (which honors
> `LEGACY_START`), not by running the module directly, or the two will disagree.

> **Gotcha — `run --start` is a *publish-time slice*, not a computation bound.**
> `update_martinez_stage run --start ...` always runs `prepare`/`qaqc`/`transition` over
> **full history internally** regardless of `--start` — that flag only trims what gets
> *written* to `paths.FINAL`, applied as the very last step in `transition_martinez_stage.py`
> after the blend, plot, and gap-check all use the full (unsliced) series. This is
> deliberate: `martinez_stage.estimate_slow_offset` estimates the slow DWR/NOAA datum
> offset with **centered** rolling windows (`offset_win=45d`, then `offset_smooth_win=10d`
> on top), and `transition` blends the frozen legacy segment against the corrected segment
> over a **fixed** window, `2013-12-20 → 2014-01-01` (`TRANSITION_START`/`TRANSITION_END`
> in `transition_martinez_stage.py`; [§2.2](#22-backup-reconstruction-when-noaa-also-fails)
> covers backup-filling). If either of those computations only saw data starting near
> `--start`, that start would become a new, artificially one-sided edge — moving the
> precarious border around rather than removing it. Always computing full history and
> slicing only the write sidesteps that entirely: no `--start` value for `run` can ever
> bias the splice or trip an edge effect, and a scheduled job can safely use a small,
> fixed `--start` (e.g. `2020-01-01`) purely to keep its staged/published artifact small,
> with no risk to correctness.
>
> This guarantee is specific to the **chained `run` command**. The standalone `prepare`/
> `qaqc` subcommands are unaffected and still use `--start`/`--end` to bound what they
> actually fetch/compute (useful for a fast ad hoc partial refresh) — so if you manually
> chain `prepare --start <recent-date>` → `qaqc` → the standalone `transition` subcommand,
> `martinez_flags.csv` genuinely only covers that bounded window, and the margin guard in
> `transition()` applies: it requires the corrected series to start at least
> `45d + 10d = 55d` before `2013-12-20` (i.e. `<= 2013-10-26`), raising an explicit
> `ValueError` naming the required date if it doesn't. That guard is derived from
> `martinez_stage.Params` at import time, so it stays correct if `offset_win`/
> `offset_smooth_win` are ever retuned.
>
> Because a scheduled `run --start <cutoff>` writes only `[cutoff, now)`, its staged
> output never overlaps anything before `cutoff` — so the dropbox reconcile policy
> (`prefer: staged` vs `prefer: repo`) is moot for everything before `cutoff`; only the
> small post-`cutoff` tail is ever reconciled, repeatedly, and `prefer: staged` there is
> fine since a full-history run stays internally self-consistent end to end. Moving
> `cutoff` forward later (e.g. `2020-01-01` → `2025-01-01`), or the first run after a
> reliability fix like this one, calls for a one-off *coherence sweep* instead: run
> `update_martinez_stage run` with `--start` omitted (writes the full, unsliced product),
> then reconcile that single file manually with a temporary `prefer: repo` override in
> the recipe (so the sweep can only fill gaps in already-published history, never
> overwrite it, until it's been validated) before reverting the recipe to `prefer: staged`
> for routine runs.

### ② Current workflow — re-run to advance to the current year

Driven by the **`update_martinez_stage.py`** orchestrator
(see [§0](#0-what-to-run-operator-quick-start)), which runs these stages in order:

1. **`prepare_mrz_data.py`** — fetch from `dms_datastore` and write canonical CSVs.
2. **`martinez_stage.py`** — QA/QC diagnostics (clock-shift lag, residual-IQR spike,
   flat-derivative/stuck-sensor, subtidal disagreement, gap count), mask + NOAA fill in the
   aligned frame → `dms_mrz_elev_2013_9999.csv`, `martinez_flags.csv`, `martinez_intervals.csv`.
3. **`transition_martinez_stage.py`** — `transition_ts` blend over
   `2013-12-20 → 2014-01-01` → `dms_mrz_elev_filled.csv`.

No manual rename is needed in the current scripts: `mrz_legacy_fill.py` writes
`mrz_stage_filled_legacy.csv`, and `transition_martinez_stage.py` reads that same file.

### ③ Production file set vs extraneous files

Use this as the retention policy when cleaning the workspace.

**Directory layout.** The pipeline is organized into three tiers (centralized in
`paths.py`) so frozen inputs, per-run scratch, and deliverables stay separate:

- **`data/` — frozen, deployable inputs.** Version-controlled artifacts that are
  never cleared automatically: the harmonic source (`mrzastro_1920_2035.csv`), the
  hand-cleaned legacy record (`dms_mrz_cleaned_1990_2017.csv`), the DFM parameters
  (`dfm_trimbur_rw_mrz_sfsub.yaml`), and the frozen pre-2014 legacy fill
  (`mrz_stage_filled_legacy.csv`). This tier is **bundled inside the installed
  package** at `src/martinez_stage_qa/data/` (declared via
  `[tool.setuptools.package-data]` in `pyproject.toml` and resolved through
  `importlib.resources` in `paths.py`), so it ships with the install and is found
  regardless of the current working directory. The legacy fill is only rewritten on
  an explicit `--rebuild-legacy` / `legacy` run.
- **`session_data/` — ephemeral per-run fetches.** The sliced source series that
  `prepare` fetches from the repo each run: `mrz_dwr_repo.csv`, `mrz_harmonic.csv`,
  `mrz_noaa_martinez.csv`, `sf_stage.csv`, `mal_stage.csv`. The whole directory is
  **cleared and respawned at the start of every `prepare` run** and is excluded from
  deployment — never hand-edit or depend on its contents between runs.
- **`output/` — products.** Final and review deliverables, but only some of it is
  version-controlled. `dms_mrz_elev_2013_9999.csv`, `dms_mrz_elev_filled.csv`, and
  `martinez_flags.csv` are DVC-tracked (via `.dvc` pointer files; the actual bytes
  are `.gitignore`d and live in the DVC remote). `martinez_intervals.csv`,
  `martinez_qaqc.png`, `martinez_qaqc_zoom.png`, `transition_martinez.png`, and any
  `martinez_naninspect_*.png` / `martinez_backupfill_*.png` are regenerated fresh
  every run and are `.gitignore`d outright — they are local review artifacts only,
  never committed to git or DVC. (These four were briefly committed to git by
  mistake; that broke a long-lived CI checkout's `git pull` when the checkout's own
  regenerated copies collided with an incoming commit — see the retention note
  below.)

Keep (required inputs, generated intermediates, and final products):
- Package modules (under `src/martinez_stage_qa/`): `update_martinez_stage.py`,
  `prepare_mrz_data.py`, `martinez_stage.py`, `transition_martinez_stage.py`,
  `mrz_legacy_fill.py`, `paths.py`
- Everything under `data/`, plus the DVC-tracked products written to `output/`
  (`dms_mrz_elev_2013_9999.csv`, `dms_mrz_elev_filled.csv`, `martinez_flags.csv`).
- `session_data/` is regenerated by `prepare`; its contents are disposable but the
  directory itself is expected.

Never commit directly to git (regenerated every run; `.gitignore`d — see §4③ above):
- `output/martinez_intervals.csv`, `output/martinez_qaqc.png`,
  `output/martinez_qaqc_zoom.png`, `output/transition_martinez.png`,
  `output/martinez_naninspect_*.png`, `output/martinez_backupfill_*.png`
- A CI checkout that ends up with one of these already committed from before this
  policy needs a hard reset (`git fetch && git reset --hard origin/<branch>`), not
  a plain `git pull`, to recover — a locally dirty tracked path can still block a
  merge even after the path is removed from git upstream.

Safe to archive/remove (obsolete names or transient junk not read by current scripts):
- Obsolete-name product: `dms_mrz_stage_filled_legacy_1990_2013.csv`
  (current scripts read/write `data/mrz_stage_filled_legacy.csv`)
- Transient / editor / OS junk: `run_out.txt` (captured stdout), `Thumbs.db`,
  `__pycache__/`, `logs/`, `logger_datastore.log`

Recommended cleanup approach:
- Move candidate files to an archive folder first (for example `archive/`), run §0.1,
  and confirm outputs and plots are unchanged except for the expected new end-date extension.

## 5. Hardwire audit — which constants are OK, which are not

| Hardwire | Location | Verdict | Rationale |
|---|---|---|---|
| `NAVD = 2.68`, `to_navd = 2006-01-01` | prepare / legacy | ✅ OK | Physical datum correction |
| `NOAA_START` (2013 / 2014-05-10) | prepare / legacy | ✅ OK | Real gauge-availability boundary |
| Transition window `2013-12-20 → 2014-01-01` | transition | ✅ OK | Fixed physical splice point |
| `M2FT` unit conversion | prepare | ✅ OK | Physical constant |
| Legacy fill `end = NOAA_START` | legacy | ✅ OK | Legacy domain is frozen |
| **`end = 2026-01-02`** | `prepare_mrz_data.py` | ❌ Config | The moving cutoff — must be a parameter |
| ~~Year-stamped CSV names~~ | prepare / all readers | ✅ Resolved | Now un-dated names under `session_data/` (see below) |

**Key distinction (resolved).** The **fetches themselves** —
`read_ts_repo("mrz", "elev", "upper", …)`, `"mrz2"`, `"sffpx"`, `"mal"` — are best
practice and stay. The earlier problem was that their **outputs had been named after a
year**, so a live-updating fetch written to a `_2025` file lied the moment the data
advanced. That is now fixed: `prepare` writes stable, un-dated names into the ephemeral
`session_data/` directory (`mrz_dwr_repo.csv`, `mrz_noaa_martinez.csv`, `sf_stage.csv`,
`mal_stage.csv`, `mrz_harmonic.csv`), with `start`/`end` as parameters. The old
year-stamped duplicates have been deleted.

## 6. Do we still need harmonic estimates?

```mermaid
flowchart TD
    H[mrzastro harmonic] --> LGuse[Legacy fill:<br/>tidal-residual fill pre-NOAA]
    H --> Curuse[Current QA/QC:<br/>clock-shift lag ref + resid-IQR ref]
    LGuse -->|essential| Keep1[Keep — no NOAA before 2014]
    Curuse -->|optional| Maybe[NOAA can serve as tidal reference]
```

- **Legacy (pre-2014): essential.** There is no NOAA gauge, so the harmonic reconstruction
  is the only independent tidal reference for filling the tidal residual band.
- **Current (post-2013): optional.** Harmonic is used only as a *reference* — for the
  windowed clock-shift lag (`ztid_harm`) and the residual-IQR spike diagnostic. NOAA already
  provides an independent, observed tidal reference and could take over both roles.
- **Recommendation:** keep harmonic on the legacy path; treat it as an *optional diagnostic*
  on the current path. It is cheap and useful precisely when NOAA is also suspect, so a full
  removal is a judgment call.

## 7. Automation & fail-fast on stale data

When runs are automated, `end` changes every invocation while the legacy system stays fixed.
That is fine — only steps ① prepare → ② QA/QC → ③ transition re-run. The real risk is a
**silent short-fetch**: if you request `end = 2026-08-05` but a source has not updated,
`read_ts_repo` simply returns a series that ends earlier, and everything downstream succeeds
on truncated data.

**Where the current code is blind.** `prepare_mrz_data.py` only checks the NOAA *start*
(`df_noaa.index.min() < NOAA_START - 7d`). Nothing checks the *end* / freshness of any source.

**Proposed guard.** For each fetched source with last valid timestamp $t_{\text{last}}$,
requested end $t_{\text{req}}$, and per-source tolerance $\tau$:

$$
t_{\text{req}} - t_{\text{last}} > \tau \;\Rightarrow\; \textbf{fail (or warn)}.
$$

Each source's tolerance $\tau$ should reflect that feed's real update latency, which we have
not yet characterized empirically — so $\tau$ is a **separately tunable per-source parameter**
rather than a fixed assumption. Start with a small default and loosen only for a source that
demonstrably lags; if a run chokes on a healthy feed, widen that source's $\tau$. (Avoid
baked-in guesses about relative NOAA vs. DWR latency; set them from observed behavior.)
Two failure modes to catch:

1. **Trailing shortfall** — the series ends well before $t_{\text{req}}$.
2. **Trailing gap** — the series *reaches* $t_{\text{req}}$ but the last stretch is mostly
   NaN. Guard with a max trailing-NaN fraction over, say, the final 7 days.

```mermaid
flowchart TD
    R[request end t_req] --> F[fetch source]
    F --> C1{t_req - t_last > tau?}
    C1 -- yes --> X[FAIL FAST: source stale]
    C1 -- no --> C2{trailing NaN frac > limit?}
    C2 -- yes --> X2[FAIL FAST: trailing gap]
    C2 -- no --> OK[proceed]
```

**Which sources warrant a hard fail vs a warning?**
- **Hard fail:** NOAA and DWR repo — they are on the critical fill/correction path; stale
  input silently corrupts the product.
- **Warn only:** SF and MAL — used as neighbors/gap-fillers; a short tail degrades but does
  not invalidate the fill, so a warning plus a recorded provenance note is enough.
- **N/A:** harmonic (`mrzastro`) extends to 2035 and is deterministic — no freshness concern.

## 8. Reviewer's guide

For a practitioner reviewing a run. Open, in order: `martinez_qaqc.png` (overview),
`martinez_qaqc_zoom.png` (last 21 days), `martinez_intervals.csv` (flagged spans + reasons),
`martinez_flags.csv` (per-timestamp diagnostics), `transition_martinez.png` (the 2013→2014
splice), and the products `dms_mrz_elev_2013_9999.csv` / `dms_mrz_elev_filled.csv`. If the run
used `--plot-orig-data`, also check any `martinez_backupfill_*.png`
([§2.2](#22-backup-reconstruction-when-noaa-also-fails)) — these mark spans where DWR and NOAA
were both unavailable and the product had to fall back to the MAL/SF/harmonic reconstruction.

### 8.1 Which reference feeds which step

Each QA "tripwire" has a physical meaning and leans on a specific reference — knowing which
tells you *what* a flag is really asserting.

| Step / tripwire | Reference used | What it means when it fires |
|---|---|---|
| Gap fill (tide in gaps) | **NOAA** | tide reconstructed from NOAA (scaled by $b$) |
| Slow drift / offset $\delta$ | **NOAA** subtidal | month-scale datum difference removed then restored |
| Clock-shift lag | **Harmonic** tidal band | DWR timestamps/clock drifted vs. astronomical tide |
| Residual-IQR spike | **Harmonic** (DWR side) + **NOAA** (comparator) | DWR tidal-residual energy abnormally high (noisy sensor) |
| Flat-derivative | **NOAA** tidal band | DWR too smooth / stuck vs. NOAA (dead or corner-cut sensor) |
| Subtidal disagreement | **NOAA** subtidal | real low-frequency divergence (datum, biofouling) |
| Gap count | none (intrinsic DWR) | coverage / how much fill is being relied on |
| SF, MAL | **legacy path**, plus modern **backup** ([§2.2](#22-backup-reconstruction-when-noaa-also-fails)) | pre-2014 neighbors; in the modern path, used only when DWR+NOAA overlap gaps leave the normal fill nothing to draw on |

> Subtlety: the residual-IQR ratio is not a clean like-for-like — the DWR residual is measured
> against the *harmonic* (`DWR − harmonic`) while the NOAA comparator uses *its own subtidal*
> (`NOAA − zsub_noaa`). It is calibrated empirically to its `resid_ratio_thresh = 0.3`, not
> physically unit-matched. If the harmonic is ever dropped from the modern path (see Open
> questions), NOAA would have to supply the DWR baseline too.

### 8.2 Reading the QA/QC plot (four panels)

| Panel | Healthy | Red flag |
|---|---|---|
| 1 — water level (raw / filtered / masked / **corrected** + NOAA + harmonic) | corrected tracks DWR where good; follows NOAA-shaped tide across shaded gaps with **no step at gap edges** | steps at gap edges; corrected diverging from *both* DWR and NOAA |
| 2 — best lag (min), ±40 min lines | scatter inside ±40 min | persistent excursion beyond ±40 min → clock/timestamp shift |
| 3 — variability ratios | resid-IQR ratio below 0.3; d/dt ratio above 0.42 | resid ≫ 0.3 → noisy DWR; d/dt ≪ 0.42 → too smooth / stuck |
| 4 — subtidal diff (DWR aligned − NOAA), ±0.07 ft | hugs zero, within ±0.07 ft | sustained excursion → real datum/biofouling disagreement |

### 8.3 Cross-file sanity checks

- **`offset_slow`** should be smooth and roughly within $-0.2 \ldots +0.6$ ft. A sudden jump is
  a datum/re-referencing event (investigate), not sensor noise.
- **Shaded intervals** in panel 1 must match rows in `martinez_intervals.csv`; skim the
  `reason` column and confirm each is plausible against the raw trace.
- **`gap_count` / `bad_dwr_fill`** — how much of the recent window is NOAA-filled vs. native
  DWR. Heavy recent fill → weight confidence accordingly.
- **`backup_filled`** — samples rescued by the MAL/SF/harmonic backup
  ([§2.2](#22-backup-reconstruction-when-noaa-also-fails)) because DWR and NOAA were both
  unavailable. Should normally be all-zero/rare; a nonzero run means the NOAA-based fill had
  nothing to draw on and the reconstruction is one step further from direct observation —
  cross-check against the corresponding `martinez_backupfill_*.png`.
- **Transition plot** — the blended line must be continuous across Dec 20 → Jan 1, no step.
- **Product files** — index monotonic, regular 15-min, no unexpected NaNs; `value` equals
  `mrz_elev_corrected`.

### 8.4 Three things to remember when interpreting

1. **The trailing ~4 days are provisional.** The 40 h subtidal filter cannot see them, so the
   drift term is *held* (not freshly estimated) and the subtidal/lag tripwires are blind there.
2. **Gap tide follows NOAA's phase**, not Martinez's (no phase-lag term). Fine for short gaps;
   be suspicious of long gaps during events.
3. **Freshness first.** Check the run log — if NOAA or DWR tripped the staleness guard the
   product is short/truncated and nothing downstream will warn you again.

### 8.5 One independent check — read it from the run log

The QA/QC step (`martinez_stage.run`) **prints the neighbor-fill regression** each run:

```
NOAA neighbor-fill regression (DWR_aligned ~ a + b*NOAA):
  b (tidal amplification) = 1.0189
  a (intercept, ft)       = -0.0692
  R^2                     = 0.99794
  sigma_resid (ft)        = 0.0741
  overlap points          = 399040
  overlap through         = 2026-06-28 00:00:00
```

Confirm $b \approx 1.0\text{–}1.02$, $a$ within a few hundredths of a foot, and
$R^2 \gtrsim 0.99$. A drifting $b$ or collapsing $R^2$ means the NOAA↔DWR relationship broke
and the fill can't be trusted — the run also prints a `[check]` warning line when $R^2 < 0.95$
or $|b-1| > 0.05$, so a bad fit is hard to miss.

