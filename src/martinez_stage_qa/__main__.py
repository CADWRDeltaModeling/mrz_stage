"""Enable ``python -m martinez_stage_qa`` as an alias for the console script.

Mirrors ``dms_datastore``'s ``__main__.py``: it simply dispatches to the Click
group so the pipeline is runnable without the installed entry point on PATH.
"""
from .update_martinez_stage import update_martinez_stage

if __name__ == "__main__":
    update_martinez_stage()
