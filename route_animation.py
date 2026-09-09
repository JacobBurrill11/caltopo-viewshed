"""
Route viewshed export: sample a hiking route at fixed distance intervals,
compute a viewshed at each sample point, and write every result into one
GeoJSON FeatureCollection.

That single file drives two consumers: an interactive Leaflet viewer
(route_viewshed_viewer.html) that scrubs between samples, and CalTopo
directly -- any one sample's polygon can be imported exactly like the
single-point workflow in viewshed.py.

Reuses viewshed.py's DEM I/O and algorithm rather than duplicating it;
viewshed.py itself is not modified.

Each sample point fetches its own small DEM tile (dem_fetch.py), sized to
just 2 * max_radius_m and discarded after use -- this decouples DEM
resolution entirely from route length, so a route of any length just
means more equally well-resolved tiles, never a bigger or coarser one.

Sections:
  1. Constants / parameters
  2. Route loading and sampling (GPX parsing, distance-based sampling)
  3. Per-sample export
  4. CLI entry point
"""

import argparse
import json
import os
import tempfile

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import LineString, Point, shape

import gpxpy

from dem_fetch import compute_required_bbox, fetch_and_reproject_dem
from terrain_features import fetch_named_features
from viewshed import (
    load_dem,
    coords_to_pixel,
    bilinear_interpolate,
    compute_viewshed,
    polygonize_visibility,
)


# ---------------------------------------------------------------------------
# 1. Constants / parameters
# ---------------------------------------------------------------------------

GPX_PATH = "route.gpx"

STEP_DISTANCE_M = 804.672       # 0.5 miles
EYE_HEIGHT_M = 1.7
TARGET_HEIGHT_M = 0.0
MAX_RADIUS_M = 32000.0          # each sample fetches its own DEM tile at a fixed 2048x2048px budget,
                                 # so resolution (~31.25m/pixel at this radius) depends only on
                                 # max_radius_m, never on route length -- see dem_fetch.py

PEAK_LIMIT = 10                 # GNIS's Summit feature class includes every named bump, not just
                                 # notable mountains -- a wide-open viewshed can turn up hundreds.
                                 # Ranking by real elevation (sampled from the DEM, since GNIS doesn't
                                 # provide it) and keeping only the tallest N keeps the list useful
                                 # without a fixed elevation cutoff that wouldn't generalize outside
                                 # the Sierra. Lakes are left unfiltered -- GNIS/OSM lake counts
                                 # haven't shown the same problem.

OUTPUT_PATH = "route_viewsheds.geojson"

MILES_PER_METER = 1 / 1609.34
SECONDS_PER_DEM_FETCH_ESTIMATE = 4.0         # network-dependent; measured ~12.2s/sample all-in
SECONDS_PER_VIEWSHED_COMPUTE_ESTIMATE = 8.0  # ~flat across this radius range with parallelization
SECONDS_PER_VIEWSHED_ESTIMATE = SECONDS_PER_DEM_FETCH_ESTIMATE + SECONDS_PER_VIEWSHED_COMPUTE_ESTIMATE


# ---------------------------------------------------------------------------
# 2. Route loading and sampling
# ---------------------------------------------------------------------------

def load_route_points(gpx_path):
    """
    Parse a GPX file into an ordered list of (lon, lat) points.

    Uses gpx.tracks[0], concatenating all of its segments in order (a
    documented simplification -- multi-track GPX files have every track
    after the first ignored, with a printed notice). Falls back to
    gpx.routes[0] if the file has no tracks at all. Raises ValueError if
    neither tracks nor routes are present.
    """
    with open(gpx_path) as f:
        gpx = gpxpy.parse(f)

    if gpx.tracks:
        if len(gpx.tracks) > 1:
            print(f"Note: GPX has {len(gpx.tracks)} tracks; using tracks[0], ignoring the rest.")
        track = gpx.tracks[0]
        points = [(pt.longitude, pt.latitude) for seg in track.segments for pt in seg.points]
    elif gpx.routes:
        points = [(pt.longitude, pt.latitude) for pt in gpx.routes[0].points]
    else:
        raise ValueError(f"{gpx_path} has no tracks or routes")

    if not points:
        raise ValueError(f"{gpx_path} was parsed but contains no points")

    return points


def utm_crs_from_lonlat(lon, lat):
    """
    Return the EPSG code for the UTM zone containing (lon, lat).

    Standard formula: zones are 6 degrees wide starting at -180; northern
    zones are EPSG:326XX, southern zones EPSG:327XX. Good for any point in
    the continental US (no antimeridian edge case applies domestically).

    Lets the DEM fetch/reprojection pipeline pick the correct zone
    automatically instead of requiring a human to know and supply it via
    --utm-crs.
    """
    zone = int((lon + 180) // 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return f"EPSG:{epsg}"


def route_to_utm_linestring(route_lonlat, crs):
    """
    Reproject the route's (lon, lat) points into `crs` and return them as
    one shapely LineString, so `.length` and `.interpolate()` give true
    meters instead of degrees.
    """
    lons = [p[0] for p in route_lonlat]
    lats = [p[1] for p in route_lonlat]
    xs, ys = warp_transform("EPSG:4326", crs, lons, lats)
    return LineString(list(zip(xs, ys)))


def sample_distances(line, step_m):
    """
    Return the list of cumulative distances (meters) along `line` to sample
    at, from 0 to line.length in step_m increments, always including the
    final length even if it's not an exact multiple of step_m.

    A route shorter than step_m still returns exactly two samples: the
    start and the end. Raises ValueError on a zero-length route.
    """
    length = line.length
    if length <= 0:
        raise ValueError("route has zero length -- check for duplicate points or a single-point GPX")

    if length < step_m:
        return [0.0, length]

    distances = [float(d) for d in np.arange(0, length, step_m)]
    if abs(distances[-1] - length) > 1e-6:
        distances.append(length)
    return distances


def interpolate_route_point(line, distance, utm_crs):
    """
    Interpolate `line` (in utm_crs meters) at `distance` and return both
    the UTM (x, y) and the reprojected EPSG:4326 (lon, lat).

    Pure geometry -- no DEM involved. Runs before that sample's DEM tile
    even exists, since the tile's own bbox is centered on this point.
    """
    pt = line.interpolate(distance)
    lons, lats = warp_transform(utm_crs, "EPSG:4326", [pt.x], [pt.y])
    return pt.x, pt.y, lons[0], lats[0]


def observer_from_point(dem, transform, nodata, x, y, eye_height_m=EYE_HEIGHT_M):
    """
    Bilinear-sample ground elevation at UTM point (x, y) from an
    already-loaded DEM (that sample's own freshly-fetched tile).

    Always uses DEM elevation, never GPX elevation -- GPX elevation tags
    are frequently absent, barometric, or noisy, and mixing them in would
    undermine the curvature-corrected geometry already validated against
    CalTopo's own viewshed tool.

    Returns None (with a printed warning left to the caller) if the point
    falls on nodata or outside the tile's bounds.
    """
    row_f, col_f = coords_to_pixel(x, y, transform)
    ground_z = bilinear_interpolate(dem, row_f, col_f, nodata)
    if ground_z is None:
        return None
    return {
        "row": int(round(row_f)),
        "col": int(round(col_f)),
        "ground_z": ground_z,
        "observer_z": ground_z + eye_height_m,
    }


# ---------------------------------------------------------------------------
# 3. Per-sample export
# ---------------------------------------------------------------------------

def estimate_runtime(num_samples, seconds_per=SECONDS_PER_VIEWSHED_ESTIMATE):
    total_min = num_samples * seconds_per / 60
    print(f"{num_samples} samples x ~{seconds_per:.0f}s (fetch + compute, approximate -- "
          f"depends on network conditions) = ~{total_min:.1f} min estimated")


def export_route_viewsheds(route_utm_line, route_lonlat, distances, utm_crs,
                            max_radius_m, target_height_m, out_path,
                            eye_height_m=EYE_HEIGHT_M, on_progress=None):
    """
    Compute a viewshed at every sample distance and write them all into one
    GeoJSON FeatureCollection: one Feature per sample (polygon + distance/
    elevation/position properties) plus one Feature for the route itself
    (a constant LineString, for the viewer to draw as background).

    Each sample fetches its own small DEM tile (dem_fetch.fetch_and_reproject_dem,
    sized to 2 * max_radius_m, independent of route length), uses it, and
    discards it -- scratch tiles live in a temporary directory that's
    cleaned up automatically, with a per-sample delete as well so they
    don't pile up mid-run on a long route. A fetch failure or nodata
    sample is skipped (printed warning), same as today's nodata handling,
    not treated as fatal for the whole route. If every sample fails,
    raises RuntimeError rather than silently writing a route-line-only
    GeoJSON that would look like a success.

    on_progress, if given, is called as on_progress(completed, total) once
    per sample -- whether that sample succeeded or was skipped -- right
    after it's been handled, so a caller (the web app) can report progress
    without this function knowing anything about Flask or HTTP.
    """
    features = []
    total = len(distances)

    route_bbox = compute_required_bbox(route_lonlat, max_radius_m, utm_crs)
    try:
        named_features = fetch_named_features(
            (route_bbox["min_lon"], route_bbox["min_lat"], route_bbox["max_lon"], route_bbox["max_lat"])
        )
        print(f"Found {len(named_features)} named peaks/lakes nearby "
              f"(candidates -- most won't actually be visible from any single sample).")
    except RuntimeError as e:
        print(f"  named feature lookup failed, continuing without peaks/lakes ({e})")
        named_features = []

    with tempfile.TemporaryDirectory(prefix="viewshed_scratch_") as scratch_dir:
        for i, distance in enumerate(distances):
            mile = distance * MILES_PER_METER
            x, y, lon, lat = interpolate_route_point(route_utm_line, distance, utm_crs)
            tmp_dem_path = os.path.join(scratch_dir, f"sample_{i:04d}.tif")

            try:
                fetch_and_reproject_dem([(lon, lat)], max_radius_m, utm_crs, tmp_dem_path)
                dem, transform, _crs, pixel_size_m, nodata = load_dem(tmp_dem_path)

                obs = observer_from_point(dem, transform, nodata, x, y, eye_height_m)
                if obs is None:
                    print(f"  skipping sample at mile {mile:.2f}: no elevation data")
                    if on_progress:
                        on_progress(i + 1, total)
                    continue

                print(f"[{i + 1}/{len(distances)}] mile {mile:.2f}: computing viewshed...")
                visibility = compute_viewshed(
                    dem, transform, pixel_size_m,
                    obs["row"], obs["col"], obs["observer_z"],
                    max_radius_m, target_height_m, nodata,
                )
                geometry = polygonize_visibility(visibility, transform, utm_crs)
            except (ValueError, RuntimeError) as e:
                print(f"  skipping sample at mile {mile:.2f}: DEM fetch failed ({e})")
                if on_progress:
                    on_progress(i + 1, total)
                continue
            finally:
                if os.path.exists(tmp_dem_path):
                    os.remove(tmp_dem_path)

            sample_polygon = shape(geometry)
            visible_features = [
                f for f in named_features
                if sample_polygon.contains(Point(f["lon"], f["lat"]))
            ]

            peaks, lakes = [], []
            for f in visible_features:
                (peaks if f["type"] == "peak" else lakes).append(f)

            ranked_peaks = []
            for f in peaks:
                peak_x, peak_y = warp_transform("EPSG:4326", utm_crs, [f["lon"]], [f["lat"]])
                peak_row, peak_col = coords_to_pixel(peak_x[0], peak_y[0], transform)
                peak_elevation = bilinear_interpolate(dem, peak_row, peak_col, nodata)
                if peak_elevation is not None:
                    ranked_peaks.append((peak_elevation, f))
            ranked_peaks.sort(key=lambda pair: pair[0], reverse=True)

            visible_features = [f for _, f in ranked_peaks[:PEAK_LIMIT]] + lakes

            features.append({
                "type": "Feature",
                "properties": {
                    "index": i,
                    "distance_m": distance,
                    "distance_mi": mile,
                    "elevation_m": float(obs["ground_z"]),
                    "lon": lon,
                    "lat": lat,
                    "visible_features": visible_features,
                },
                "geometry": geometry,
            })
            if on_progress:
                on_progress(i + 1, total)

    if not features:
        raise RuntimeError(f"All {len(distances)} samples failed -- no viewshed could be computed")

    features.append({
        "type": "Feature",
        "properties": {"name": "route"},
        "geometry": {
            "type": "LineString",
            "coordinates": [[lon, lat] for lon, lat in route_lonlat],
        },
    })

    feature_collection = {"type": "FeatureCollection", "features": features}
    with open(out_path, "w") as f:
        json.dump(feature_collection, f)

    print(f"Wrote {out_path} ({len(features) - 1} viewshed samples + 1 route line)")


def export_point_viewshed(lon, lat, max_radius_m, target_height_m, out_path,
                           eye_height_m=EYE_HEIGHT_M, utm_crs=None):
    """
    Compute a single-point viewshed and write it as a one-Feature GeoJSON
    FeatureCollection, in the exact same Feature property shape a sample
    in export_route_viewsheds' output uses (index, distance_m/distance_mi
    both 0.0, lon, lat, elevation_m, visible_features) -- so this can be
    served under the same route_viewsheds.geojson filename and rendered by
    the same result.html/viewer.js with no structural changes. No "route"
    LineString Feature is written; viewer.js already falls back to
    map.setView() centered on the single sample when one isn't present.

    Deliberately duplicates (rather than factors out a shared helper with)
    export_route_viewsheds' per-sample body -- the two differ enough (mile
    bookkeeping vs. none) that a shared interface would be more awkward
    than the ~30 lines this saves, and this keeps zero regression risk to
    the already-tested route pipeline, which is untouched.

    Raises RuntimeError if the DEM fetch fails or the point falls on
    nodata -- there's no "skip this sample and continue" fallback for a
    single point the way there is for a route.
    """
    utm_crs = utm_crs or utm_crs_from_lonlat(lon, lat)

    route_bbox = compute_required_bbox([(lon, lat)], max_radius_m, utm_crs)
    try:
        named_features = fetch_named_features(
            (route_bbox["min_lon"], route_bbox["min_lat"], route_bbox["max_lon"], route_bbox["max_lat"])
        )
    except RuntimeError as e:
        print(f"  named feature lookup failed, continuing without peaks/lakes ({e})")
        named_features = []

    with tempfile.TemporaryDirectory(prefix="viewshed_scratch_") as scratch_dir:
        tmp_dem_path = os.path.join(scratch_dir, "point.tif")
        try:
            fetch_and_reproject_dem([(lon, lat)], max_radius_m, utm_crs, tmp_dem_path)
            dem, transform, _crs, pixel_size_m, nodata = load_dem(tmp_dem_path)

            x, y = warp_transform("EPSG:4326", utm_crs, [lon], [lat])
            obs = observer_from_point(dem, transform, nodata, x[0], y[0], eye_height_m)
            if obs is None:
                raise RuntimeError("no elevation data at that point")

            visibility = compute_viewshed(
                dem, transform, pixel_size_m,
                obs["row"], obs["col"], obs["observer_z"],
                max_radius_m, target_height_m, nodata,
            )
            geometry = polygonize_visibility(visibility, transform, utm_crs)
        finally:
            if os.path.exists(tmp_dem_path):
                os.remove(tmp_dem_path)

    sample_polygon = shape(geometry)
    visible_features = [
        f for f in named_features
        if sample_polygon.contains(Point(f["lon"], f["lat"]))
    ]

    peaks, lakes = [], []
    for f in visible_features:
        (peaks if f["type"] == "peak" else lakes).append(f)

    ranked_peaks = []
    for f in peaks:
        peak_x, peak_y = warp_transform("EPSG:4326", utm_crs, [f["lon"]], [f["lat"]])
        peak_row, peak_col = coords_to_pixel(peak_x[0], peak_y[0], transform)
        peak_elevation = bilinear_interpolate(dem, peak_row, peak_col, nodata)
        if peak_elevation is not None:
            ranked_peaks.append((peak_elevation, f))
    ranked_peaks.sort(key=lambda pair: pair[0], reverse=True)

    visible_features = [f for _, f in ranked_peaks[:PEAK_LIMIT]] + lakes

    feature_collection = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "index": 0,
                "distance_m": 0.0,
                "distance_mi": 0.0,
                "elevation_m": float(obs["ground_z"]),
                "lon": lon,
                "lat": lat,
                "visible_features": visible_features,
            },
            "geometry": geometry,
        }],
    }
    with open(out_path, "w") as f:
        json.dump(feature_collection, f)

    print(f"Wrote {out_path} (1 point viewshed)")


# ---------------------------------------------------------------------------
# 4. CLI entry point
# ---------------------------------------------------------------------------

def run_export(args):
    route_lonlat = load_route_points(args.gpx)
    utm_crs = args.utm_crs or utm_crs_from_lonlat(*route_lonlat[0])
    line = route_to_utm_linestring(route_lonlat, utm_crs)
    distances = sample_distances(line, args.step)

    estimate_runtime(len(distances))
    export_route_viewsheds(
        line, route_lonlat, distances, utm_crs,
        args.max_radius, TARGET_HEIGHT_M, args.out,
        eye_height_m=args.eye_height,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="compute viewsheds along a route")
    export_parser.add_argument("--gpx", default=GPX_PATH)
    export_parser.add_argument("--out", default=OUTPUT_PATH)
    export_parser.add_argument("--step", type=float, default=STEP_DISTANCE_M)
    export_parser.add_argument("--max-radius", type=float, default=MAX_RADIUS_M)
    export_parser.add_argument("--eye-height", type=float, default=EYE_HEIGHT_M,
                                help="observer height above ground, in meters (default: "
                                     f"{EYE_HEIGHT_M}, roughly eye level for a standing person)")
    export_parser.add_argument("--utm-crs", default=None,
                                help="UTM zone EPSG code override, e.g. EPSG:32610. Auto-detected "
                                     "from the route's first point by default; only needed to force "
                                     "a zone (e.g. a route straddling two UTM zones).")
    export_parser.set_defaults(func=run_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
