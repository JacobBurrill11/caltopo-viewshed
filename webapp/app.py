"""
Minimal Flask front end for the route viewshed pipeline.

Upload a GPX file; everything else (bbox computation, per-sample DEM
fetch/reprojection, viewshed export) happens automatically. Each sample
point along the route fetches its own small DEM tile internally (see
export_route_viewsheds in route_animation.py) -- this app never handles
a DEM file directly.

Step 1: routes are labeled and saved (via route_store.py) so more than
one can be computed and revisited later from the dashboard at "/".
"""

import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for

import progress_store
import route_store
from route_animation import (
    MAX_RADIUS_M,
    TARGET_HEIGHT_M,
    export_point_viewshed,
    export_route_viewsheds,
    load_route_points,
    route_to_utm_linestring,
    sample_distances,
    utm_crs_from_lonlat,
)

MILES_TO_METERS = 1609.34

app = Flask(__name__)


@app.route("/")
def title():
    return render_template("title.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/routes")
def dashboard():
    return render_template("dashboard.html", routes=route_store.list_routes())


@app.route("/upload")
def upload_form():
    return render_template("upload.html")


@app.route("/run", methods=["POST"])
def run_pipeline():
    mode = request.form.get("mode", "route")

    upload = request.files.get("gpx")
    has_gpx = bool(upload and upload.filename)
    has_point = bool(request.form.get("lat", "").strip() or request.form.get("lon", "").strip())
    if has_gpx and has_point:
        # Shouldn't be reachable through the real form -- the mode dropdown
        # disables whichever section isn't active, so a browser submission
        # never carries both. Guards against a hand-crafted request or a
        # JS failure doing so anyway, regardless of which `mode` was sent.
        return render_template("error.html", message="Submitted both a GPX file and a lat/lon "
                                                       "point -- pick one or the other."), 400

    if mode == "point":
        return _run_point()
    return _run_route(upload)


def _run_route(upload):
    if not upload or upload.filename == "":
        return render_template("error.html", message="No GPX file was uploaded."), 400

    label = request.form.get("label", "").strip() or os.path.splitext(upload.filename)[0]
    eye_height_m = float(request.form.get("eye_height_m", 10.0))
    step_distance_mi = float(request.form.get("step_distance_mi", 0.5))
    step_distance_m = step_distance_mi * MILES_TO_METERS

    route_id = route_store.generate_route_id()
    this_dir = route_store.route_dir(route_id)
    os.makedirs(this_dir, exist_ok=True)

    gpx_path = os.path.join(this_dir, "route.gpx")
    upload.save(gpx_path)

    # Everything up through sampling is fast and purely local (GPX parsing,
    # UTM zone detection, distance math) -- no reason to background it. Only
    # the actual per-sample DEM fetch + viewshed compute (export_route_viewsheds,
    # the slow part) runs in a thread, so /run can redirect immediately to a
    # page that polls progress instead of blocking for minutes.
    try:
        route_lonlat = load_route_points(gpx_path)
        utm_crs = utm_crs_from_lonlat(*route_lonlat[0])
        line = route_to_utm_linestring(route_lonlat, utm_crs)
        distances = sample_distances(line, step_distance_m)
    except Exception as e:
        # Broad on purpose, unlike the narrower (ValueError, RuntimeError)
        # this project uses elsewhere for its own internal failure modes:
        # this step's input is an arbitrary user-uploaded file, and a
        # malformed one can raise a third-party parser's own exception type
        # (e.g. gpxpy.gpx.GPXXMLSyntaxException on bad XML) that isn't
        # either of those -- caught here so a bad upload always gets a
        # clean error page instead of a raw 500, with no orphaned route
        # directory left behind either way.
        route_store.delete_route(route_id)
        return render_template("error.html", message=f"Couldn't process that GPX file: {e}"), 400

    progress_store.start(route_id, len(distances))

    def run_in_background():
        out_path = os.path.join(this_dir, "route_viewsheds.geojson")
        try:
            export_route_viewsheds(
                line, route_lonlat, distances, utm_crs,
                MAX_RADIUS_M, TARGET_HEIGHT_M, out_path,
                eye_height_m=eye_height_m,
                on_progress=lambda completed, total: progress_store.update(route_id, completed, total),
            )
        except (ValueError, RuntimeError) as e:
            progress_store.fail(route_id, str(e))
            route_store.delete_route(route_id)
            return

        route_store.save_route_metadata(
            route_id, label, upload.filename, eye_height_m, step_distance_mi,
            total_distance_mi=distances[-1] / MILES_TO_METERS,
            sample_count=len(distances),
        )
        progress_store.finish(route_id)

    threading.Thread(target=run_in_background, daemon=True).start()

    return redirect(url_for("processing", route_id=route_id))


def _run_point():
    try:
        lat = float(request.form.get("lat", ""))
        lon = float(request.form.get("lon", ""))
    except ValueError:
        return render_template("error.html", message="Latitude and longitude must both be numbers."), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return render_template("error.html", message="Latitude must be between -90 and 90, "
                                                       "longitude between -180 and 180."), 400

    label = request.form.get("label", "").strip() or f"Point ({lat:.4f}, {lon:.4f})"
    eye_height_m = float(request.form.get("eye_height_m", 10.0))

    route_id = route_store.generate_route_id()
    this_dir = route_store.route_dir(route_id)
    os.makedirs(this_dir, exist_ok=True)

    progress_store.start(route_id, 1)

    def run_in_background():
        out_path = os.path.join(this_dir, "route_viewsheds.geojson")
        try:
            export_point_viewshed(lon, lat, MAX_RADIUS_M, TARGET_HEIGHT_M, out_path,
                                   eye_height_m=eye_height_m)
        except (ValueError, RuntimeError) as e:
            progress_store.fail(route_id, str(e))
            route_store.delete_route(route_id)
            return

        route_store.save_route_metadata(
            route_id, label, None, eye_height_m,
            kind="point", lon=lon, lat=lat, sample_count=1, total_distance_mi=0.0,
        )
        progress_store.finish(route_id)

    threading.Thread(target=run_in_background, daemon=True).start()

    return redirect(url_for("processing", route_id=route_id))


@app.route("/processing/<route_id>")
def processing(route_id):
    progress = progress_store.get(route_id)
    if progress is None:
        return render_template("error.html", message="That route isn't computing (or the server "
                                                       "restarted since it started)."), 404
    return render_template("processing.html", route_id=route_id)


@app.route("/routes/<route_id>/status")
def route_status(route_id):
    progress = progress_store.get(route_id)
    if progress is None:
        return jsonify({"status": "unknown"}), 404
    return jsonify(progress)


@app.route("/results/<route_id>")
def results(route_id):
    meta = route_store.get_route(route_id)
    if meta is None:
        return render_template("error.html", message="That route wasn't found."), 404
    return render_template("result.html", route_id=route_id, label=meta["label"])


@app.route("/results/<route_id>/route_viewsheds.geojson")
def results_geojson(route_id):
    return send_from_directory(route_store.route_dir(route_id), "route_viewsheds.geojson")


@app.route("/routes/<route_id>/delete", methods=["POST"])
def delete_route(route_id):
    route_store.delete_route(route_id)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    # Flask's debug-mode reloader re-execs this script as a child process
    # (setting WERKZEUG_RUN_MAIN=true there) every time it restarts a
    # server after a file change. Only opening the browser when that
    # variable is *unset* means this fires once, on the very first
    # `python3 webapp/app.py`, and not again on every reload. The short
    # delay in a background thread gives the actual server a moment to
    # start listening before the browser tries to connect.
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        threading.Timer(1.25, lambda: webbrowser.open("http://127.0.0.1:5000/")).start()

    # threaded=True: the background compute thread spawned per /run request
    # (see run_in_background above) needs the dev server able to keep
    # answering /routes/<id>/status polls concurrently, not queued behind it.
    app.run(debug=True, port=5000, threaded=True)
