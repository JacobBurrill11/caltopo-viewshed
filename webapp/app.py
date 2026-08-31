"""
Minimal Flask front end for the route viewshed pipeline.

Upload a GPX file; everything else (bbox computation, DEM fetch,
reprojection, viewshed export) happens automatically, replacing the
manual bbox/curl/reproject steps documented in the CLI README.

Step 0 scope only: single-slot storage (one route at a time, no
labeling/history -- see the project README's roadmap for later steps).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, redirect, render_template, request, send_from_directory, url_for

from dem_fetch import fetch_and_reproject_dem
from route_animation import (
    MAX_RADIUS_M,
    TARGET_HEIGHT_M,
    export_route_viewsheds,
    load_route_points,
    route_to_utm_linestring,
    sample_distances,
    utm_crs_from_lonlat,
)
from viewshed import load_dem

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
MILES_TO_METERS = 1609.34

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("upload.html")


@app.route("/run", methods=["POST"])
def run_pipeline():
    upload = request.files.get("gpx")
    if not upload or upload.filename == "":
        return render_template("error.html", message="No GPX file was uploaded."), 400

    eye_height_m = float(request.form.get("eye_height_m", 10.0))
    step_distance_mi = float(request.form.get("step_distance_mi", 0.5))
    step_distance_m = step_distance_mi * MILES_TO_METERS

    os.makedirs(DATA_DIR, exist_ok=True)
    gpx_path = os.path.join(DATA_DIR, "route.gpx")
    upload.save(gpx_path)

    try:
        route_lonlat = load_route_points(gpx_path)
        utm_crs = utm_crs_from_lonlat(*route_lonlat[0])

        dem_path = os.path.join(DATA_DIR, "dem.tif")
        fetch_and_reproject_dem(route_lonlat, MAX_RADIUS_M, utm_crs, dem_path)

        dem, transform, crs, pixel_size_m, nodata = load_dem(dem_path)
        line = route_to_utm_linestring(route_lonlat, crs)
        distances = sample_distances(line, step_distance_m)

        out_path = os.path.join(DATA_DIR, "route_viewsheds.geojson")
        export_route_viewsheds(
            dem, transform, crs, pixel_size_m, nodata,
            line, route_lonlat, distances,
            MAX_RADIUS_M, TARGET_HEIGHT_M, out_path,
            eye_height_m=eye_height_m,
        )
    except (ValueError, RuntimeError) as e:
        return render_template("error.html", message=str(e)), 400

    return redirect(url_for("results"))


@app.route("/results/current")
def results():
    geojson_path = os.path.join(DATA_DIR, "route_viewsheds.geojson")
    if not os.path.exists(geojson_path):
        return render_template("error.html", message="No route has been computed yet."), 404
    return render_template("result.html")


@app.route("/results/current/route_viewsheds.geojson")
def results_geojson():
    return send_from_directory(DATA_DIR, "route_viewsheds.geojson")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
