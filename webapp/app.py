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

from flask import Flask, redirect, render_template, request, send_from_directory, url_for

import route_store
from route_animation import (
    MAX_RADIUS_M,
    TARGET_HEIGHT_M,
    export_route_viewsheds,
    load_route_points,
    route_to_utm_linestring,
    sample_distances,
    utm_crs_from_lonlat,
)

MILES_TO_METERS = 1609.34

app = Flask(__name__)


@app.route("/")
def dashboard():
    return render_template("dashboard.html", routes=route_store.list_routes())


@app.route("/upload")
def upload_form():
    return render_template("upload.html")


@app.route("/run", methods=["POST"])
def run_pipeline():
    upload = request.files.get("gpx")
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

    try:
        route_lonlat = load_route_points(gpx_path)
        utm_crs = utm_crs_from_lonlat(*route_lonlat[0])

        line = route_to_utm_linestring(route_lonlat, utm_crs)
        distances = sample_distances(line, step_distance_m)

        out_path = os.path.join(this_dir, "route_viewsheds.geojson")
        export_route_viewsheds(
            line, route_lonlat, distances, utm_crs,
            MAX_RADIUS_M, TARGET_HEIGHT_M, out_path,
            eye_height_m=eye_height_m,
        )
    except (ValueError, RuntimeError) as e:
        return render_template("error.html", message=str(e)), 400

    route_store.save_route_metadata(
        route_id, label, upload.filename, eye_height_m, step_distance_mi,
        total_distance_mi=distances[-1] / MILES_TO_METERS,
        sample_count=len(distances),
    )

    return redirect(url_for("results", route_id=route_id))


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

    app.run(debug=True, port=5000)
