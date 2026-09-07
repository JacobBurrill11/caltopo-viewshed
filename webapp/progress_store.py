"""
In-memory progress tracking for routes computing in a background thread.

A plain module-level dict, not a database -- matches route_store.py's own
"no DB, this is a single-user local tool" philosophy. Reads/writes here are
always of one key at a time (one route_id), which CPython's GIL makes safe
without an explicit lock: no two threads can be mid-assignment to the same
dict slot simultaneously.

This state is lost on process restart (including Flask's debug-mode
reloader restarting the server on a file change) -- acceptable for a
personal local tool where that's a rare, recoverable inconvenience, not a
data-loss risk (the route itself just needs to be re-run).
"""

_progress = {}


def start(route_id, total):
    """Register a new route as running, with 0 of `total` samples done."""
    _progress[route_id] = {"status": "running", "completed": 0, "total": total, "message": None}


def update(route_id, completed, total):
    """Record progress for a route that's still running."""
    _progress[route_id] = {"status": "running", "completed": completed, "total": total, "message": None}


def finish(route_id):
    """Mark a route as finished successfully."""
    entry = _progress.get(route_id, {})
    _progress[route_id] = {
        "status": "done",
        "completed": entry.get("total", 0),
        "total": entry.get("total", 0),
        "message": None,
    }


def fail(route_id, message):
    """Mark a route as failed, with a human-readable error message."""
    entry = _progress.get(route_id, {})
    _progress[route_id] = {
        "status": "error",
        "completed": entry.get("completed", 0),
        "total": entry.get("total", 0),
        "message": message,
    }


def get(route_id):
    """
    Return the current progress dict for route_id, or None if it's
    unknown -- either it never started, or the server has restarted since.
    """
    return _progress.get(route_id)
