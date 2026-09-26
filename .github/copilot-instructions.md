# martinez-stage-qa agent guidance

This package builds the continuous, gap-filled Martinez (`mrz`) water-level
series. See `martinez_workflow.md` at the project root for the pipeline
documentation and reviewer's guide.

This is a standalone package. It depends on `dms_datastore` only for
repository data access (`read_ts`, `read_ts_repo`) — it does not import
`dms_datastore` internals for anything else, including logging (see below).

## Coding practice

Conventions adopted from `dms_datastore`'s `AGENTS.md`:

- Plan before coding.
- Keep functions single-purpose and testable.
- Do not refactor outside the scope of the requested work. Alert the user if
  broader refactoring appears warranted.
- Do not contract existing documentation; preserve NumPy-style docstrings and
  repair them when interfaces change.
- Prefer explicit errors such as `ValueError` over elaborate recovery from
  invalid arguments.
- Avoid making inference from surrounding files the only way to use an API —
  inference is a convenience, but important inputs should also be supplyable
  explicitly.
- Assume the established tool stack (click, numpy, pandas, matplotlib,
  vtools3, dms_datastore) exists; do not add elaborate defensive discovery
  for expected dependencies.
- Prefer failure to robustification and silent passes.

## CLI style

This package follows the `dms_datastore` CLI guide
(`.github/docs/CLI_GUIDE.md` in that repo). Key points as applied here:

- Use **Click** for all CLI entry points (`update_martinez_stage` is a Click
  group with `run` / `prepare` / `qaqc` / `transition` / `legacy`
  subcommands).
- Separate CLI parsing from work functions: each subcommand parses/validates
  options, configures logging, then delegates to a plain-Python work function
  (e.g. `martinez_stage.run(...)`, `transition_martinez_stage.transition(...)`)
  that takes explicit named arguments — not Click contexts or namespaces.
- Provide both long and short help: `@click.help_option("-h", "--help")` (via
  `context_settings={"help_option_names": ["-h", "--help"]}`).
- Prefer long options in kebab-case (`--rebuild-legacy`, `--plot-orig-data`).
- Use conventional option names/meanings: `--debug`, `--quiet`, `--logdir`,
  `--start`/`--end` with ISO-style datetimes (or the `NOW` token for `--end`
  here). Don't use `--sdate`.
- `--quiet` and `--debug`/verbosity are conflicting modes handled by
  `resolve_loglevel()`.
- Configure logging at the CLI entry point (see `_configure_logging()` in
  `update_martinez_stage.py`), never inside reusable work functions.

## Logging

The logging approach is **duplicated from `dms_datastore.logging_config`**
into this package's own
[src/martinez_stage_qa/logging_config.py](../src/martinez_stage_qa/logging_config.py)
rather than imported, so the package doesn't take on a runtime dependency on
`dms_datastore` internals for something as basic as logging setup. Keep it in
sync by hand if the upstream module changes in a way worth adopting — do not
re-introduce an `import dms_datastore.logging_config`.

Conventions:

- Every module gets its own logger: `logger = logging.getLogger(__name__)`.
- `martinez_stage_qa/__init__.py` attaches a `logging.NullHandler()` to the
  package logger so importing the library is silent until a CLI (or caller)
  calls `configure_logging(package_name="martinez_stage_qa", ...)`.
- CLI entry points call `resolve_loglevel(debug=..., quiet=..., loglevel=...)`
  then `configure_logging(package_name="martinez_stage_qa", level=..., 
  console=..., logdir=..., logfile_prefix=...)` — see `_configure_logging()`
  in `update_martinez_stage.py`. Every subcommand uses a distinct
  `logfile_prefix` (`update_run`, `prepare`, `qaqc`, `transition`).
- Log files are timestamped and PID-suffixed automatically; console logs to
  stderr. Don't build a parallel ad hoc logging setup for a new command —
  reuse `_logging_options` / `_configure_logging`.

<!-- mermaid-ai-skills:start -->
## Mermaid Diagrams

When the user asks to create, edit, or visualize a diagram, follow the
instructions in `.github/instructions/mermaid.instructions.md`.
<!-- mermaid-ai-skills:end -->
