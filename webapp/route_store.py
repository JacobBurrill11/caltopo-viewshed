"""
Route storage: persists saved routes to disk so the dashboard can list,
view, and delete more than one computed route.

One directory per route under webapp/data/routes/<route_id>/, each
holding that route's GPX, DEM, computed GeoJSON, and a metadata.json.
No database/index file -- list_routes() just walks the directory, which
is fine at the "dozens of routes" scale a personal single-user tool
actually sees.

app.py builds individual file paths itself via
os.path.join(route_dir(route_id), "dem.tif") etc. -- this module only
owns the route directory itself and its metadata.json.
"""

import json
import os
import datetime
import re
import secrets
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTES_DIR = os.path.join(BASE_DIR, "data", "routes")


def generate_route_id():
    """
    Generate a new route ID: "<YYYYMMDD-HHMMSS>-<6 hex chars>",
    e.g. "20260831-143205-a1b2c3".

    The timestamp prefix keeps route directories sorted chronologically
    and readable when browsing webapp/data/routes/ by hand -- you can
    tell when a route was computed without opening metadata.json. The
    random suffix (secrets.token_hex(3), stdlib) guards the one real
    collision case for a single-user app: two uploads landing in the
    same wall-clock second (e.g. a double-submit, or two browser tabs).
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(3)
    return f"{timestamp}-{suffix}"


def route_dir(route_id):
    """
    Return the absolute path to webapp/data/routes/<route_id>/.

    Must validate that route_id matches the format generate_route_id()
    produces and raise ValueError otherwise, BEFORE it's ever joined into
    a filesystem path -- a cheap guard against a malformed or malicious
    route_id (e.g. "../../etc") ever reaching a file operation or
    send_from_directory.

    Does not check whether the directory exists, and does not create it
    -- callers that need it to exist call save_route_metadata (which
    creates it) or os.makedirs directly.
    """
    if not isinstance(route_id, str):
        raise ValueError("route_id must be a string")

    if not route_id:
        raise ValueError("route_id cannot be empty")

    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", route_id):
        raise ValueError("Invalid route_id format")

    return os.path.join(ROUTES_DIR, route_id)


def save_route_metadata(route_id, label, original_filename, eye_height_m,
                         step_distance_mi=None, total_distance_mi=None, sample_count=1,
                         kind="route", lon=None, lat=None):
    """
    Create webapp/data/routes/<route_id>/ if it doesn't exist yet, and
    write metadata.json into it with:
        route_id, label, original_filename, created_at (ISO 8601 UTC,
        server-generated -- e.g. datetime.now(timezone.utc).isoformat()),
        eye_height_m, step_distance_mi, total_distance_mi, sample_count,
        kind, lon, lat

    kind is "route" (default, backward-compatible with every route saved
    before this field existed -- those files simply lack the key, and
    callers should treat a missing/absent kind as "route") or "point" for
    a single-point viewshed, which has no meaningful step/total distance
    (left None) but does have a fixed lon/lat (None for route entries,
    where the observer position varies along the route instead).

    Overwrites metadata.json if called again for an existing route_id.
    Returns None.
    """

    os.makedirs(route_dir(route_id), exist_ok=True)
    metadata = {
        "route_id": route_id,
        "label": label,
        "original_filename": original_filename,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "eye_height_m": eye_height_m,
        "step_distance_mi": step_distance_mi,
        "total_distance_mi": total_distance_mi,
        "sample_count": sample_count,
        "kind": kind,
        "lon": lon,
        "lat": lat,
    }
    metadata_path = os.path.join(route_dir(route_id), "metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        import json
        json.dump(metadata, f, indent=2)



def list_routes():
    """
    Return one metadata dict per saved route, sorted by created_at
    descending (newest first).

    Scans webapp/data/routes/*/metadata.json. A route directory with a
    missing or corrupt (unparseable) metadata.json is skipped with a
    printed warning rather than raised -- one bad route shouldn't break
    the dashboard for every other route. Returns [] if webapp/data/routes/
    doesn't exist yet or has no valid routes.
    """
    if not os.path.exists(ROUTES_DIR):
        return []
    routes = []
    for entry in os.listdir(ROUTES_DIR):
        if os.path.isdir(os.path.join(ROUTES_DIR, entry)):
            metadata_path = os.path.join(ROUTES_DIR, entry, "metadata.json")
            if os.path.exists(metadata_path):
                try:
                    with open(metadata_path, "r", encoding="utf-8") as f:
                        metadata = json.load(f)
                    routes.append(metadata)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"Warning: Corrupt metadata.json for route {entry}: {e}")
    return sorted(routes, key=lambda x: x["created_at"], reverse=True)


def get_route(route_id):
    """
    Return the metadata dict for one route_id, or None if the route
    directory doesn't exist or its metadata.json is missing/corrupt.
    Callers (Flask routes) treat None as "404, route not found."
    """
    metadata_path = os.path.join(route_dir(route_id), "metadata.json")
    if not os.path.exists(metadata_path):
        return None
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        return metadata
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: Corrupt metadata.json for route {route_id}: {e}")
        return None


def delete_route(route_id):
    """
    Delete webapp/data/routes/<route_id>/ and everything in it
    (shutil.rmtree). Returns True if a directory was found and removed,
    False if route_id didn't exist -- deleting something already gone is
    a no-op success from the caller's perspective, not an error.
    """
    if not os.path.exists(route_dir(route_id)):
        return False
    shutil.rmtree(route_dir(route_id))
    return True

