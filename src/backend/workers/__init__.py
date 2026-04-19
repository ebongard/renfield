"""Renfield worker processes (#388).

Each worker module is runnable via ``python -m workers.<name>`` and must not
import or instantiate the FastAPI app (``main.app``). Worker pods exist to
stay lean — pulling the whole API lifecycle would negate the memory budget
that motivated the split.
"""
