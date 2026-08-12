"""Martinez (mrz) continuous gap-filled stage series pipeline.

See ``martinez_workflow.md`` at the project root for the full pipeline
documentation and reviewer's guide.
"""

__version__ = "0.1.0"

import logging

# Library-friendly default: attach a no-op handler to the package logger tree so
# importing the package never emits "No handlers could be found" and produces no
# output unless a CLI (or the caller) configures logging via
# ``dms_datastore.logging_config.configure_logging(package_name="martinez_stage_qa", ...)``.
logging.getLogger(__name__).addHandler(logging.NullHandler())
