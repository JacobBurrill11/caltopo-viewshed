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

Sections:
  1. Constants / parameters
  2. Route loading and sampling (GPX parsing, distance-based sampling)
  3. Per-sample export
  4. CLI entry point
"""

import argparse
import json

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import LineString

import gpxpy

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
DEM_PATH = "route_dem.tif"      # must cover the route's bbox + MAX_RADIUS_M -- see the `bbox` subcommand
UTM_CRS = "EPSG:32611"          # must match whatever zone the fetched DEM was reprojected into

STEP_DISTANCE_M = 804.672       # 0.5 miles
EYE_HEIGHT_M = 1.7
TARGET_HEIGHT_M = 0.0
MAX_RADIUS_M = 8000.0           # smaller than viewshed.py's single-point default -- the runtime lever

OUTPUT_PATH = "route_viewsheds.geojson"

MILES_PER_METER = 1 / 1609.34
SECONDS_PER_VIEWSHED_ESTIMATE = 8.0   # measured at MAX_RADIUS_M=8000; scales roughly with radius^2


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


def compute_required_bbox(route_lonlat, max_radius_m, utm_crs=UTM_CRS):
    """
    Compute the lon/lat bounding box needed to fetch a DEM that covers the
    full route plus a max_radius_m buffer in every direction, so every
    sample point's viewshed rays stay within the fetched data.

    Steps:
      - Reproject route_lonlat into utm_crs (rasterio.warp.transform, same
        pattern as route_to_utm_linestring below).
      - Take the min/max x and min/max y of the projected points, then
        buffer those bounds outward by max_radius_m on every side.
      - Reproject the 4 buffered corners (not just the center point) back
        to EPSG:4326 and take the min/max lon/lat of the results -- UTM
        axes aren't lon/lat-aligned, so buffering in one CRS and taking a
        naive min/max in the other would clip corners.
      - Estimate the pixel dimensions needed to cover that bbox at
        roughly this project's usual resolution (~16.6 m/pixel, matching
        the existing DEM), and warn if either dimension would exceed
        ~2048px -- the empirically-found ceiling for a single USGS 3DEP
        ImageServer exportImage request (no mosaicking is implemented;
        long routes are a known v1 limitation).

    Returns a dict: {"min_lon", "min_lat", "max_lon", "max_lat",
    "est_width_px", "est_height_px"}.
    """
    lons = [p[0] for p in route_lonlat]
    lats = [p[1] for p in route_lonlat]
    xs, ys = warp_transform("EPSG:4326", utm_crs, lons, lats)
    max_x, min_x = max(xs), min(xs)
    max_y, min_y = max(ys), min(ys)
    min_x -= max_radius_m
    max_x += max_radius_m
    min_y -= max_radius_m
    max_y += max_radius_m
    top_left = warp_transform(utm_crs, "EPSG:4326", [min_x], [max_y])
    bottom_right = warp_transform(utm_crs, "EPSG:4326", [max_x], [min_y])
    top_right = warp_transform(utm_crs, "EPSG:4326", [max_x], [max_y])
    bottom_left = warp_transform(utm_crs, "EPSG:4326", [min_x], [min_y])
    min_lon = min(top_left[0][0], bottom_right[0][0], top_right[0][0], bottom_left[0][0])
    max_lon = max(top_left[0][0], bottom_right[0][0], top_right[0][0], bottom_left[0][0])
    min_lat = min(top_left[1][0], bottom_right[1][0], top_right[1][0], bottom_left[1][0])
    max_lat = max(top_left[1][0], bottom_right[1][0], top_right[1][0], bottom_left[1][0])
    est_width_px = (max_x - min_x) / 16.6
    est_height_px = (max_y - min_y) / 16.6
    if (est_width_px) > 2048 or (est_height_px) > 2048:
        print("WARNING: route + buffer exceeds ~2048px in at least one dimension; "
              "DEM export may fail. Consider a smaller --max-radius or mosaicking "
              "multiple DEM tiles (not implemented).")
    return {
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "est_width_px": est_width_px,
        "est_height_px": est_height_px
    }
    


def route_to_utm_linestring(route_lonlat, crs):
    """
    Reproject the route's (lon, lat) points into `crs` (the DEM's CRS) and
    return them as one shapely LineString, so `.length` and `.interpolate()`
    give true meters instead of degrees.
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


def observer_from_distance(dem, transform, crs, nodata, line, distance):
    """
    Interpolate the point at `distance` along `line`, look up its ground
    elevation via bilinear interpolation (not a hard cell-snap), and
    return everything needed to compute + describe a viewshed there.

    Always uses DEM elevation, never GPX elevation -- GPX elevation tags
    are frequently absent, barometric, or noisy, and mixing them in would
    undermine the curvature-corrected geometry already validated against
    CalTopo's own viewshed tool.

    Returns None (with a printed warning) if the point falls on nodata or
    outside the DEM's bounds, so the caller can skip that sample.
    """
    pt = line.interpolate(distance)
    row_f, col_f = coords_to_pixel(pt.x, pt.y, transform)
    ground_z = bilinear_interpolate(dem, row_f, col_f, nodata)

    if ground_z is None:
        print(f"  skipping sample at mile {distance * MILES_PER_METER:.2f}: no elevation data")
        return None

    lons, lats = warp_transform(crs, "EPSG:4326", [pt.x], [pt.y])

    return {
        "distance_m": distance,
        "row": int(round(row_f)),
        "col": int(round(col_f)),
        "ground_z": ground_z,
        "observer_z": ground_z + EYE_HEIGHT_M,
        "lon": lons[0],
        "lat": lats[0],
    }


# ---------------------------------------------------------------------------
# 3. Per-sample export
# ---------------------------------------------------------------------------

def estimate_runtime(num_samples, seconds_per=SECONDS_PER_VIEWSHED_ESTIMATE):
    total_min = num_samples * seconds_per / 60
    print(f"{num_samples} samples x ~{seconds_per:.0f}s = ~{total_min:.1f} min estimated")


def export_route_viewsheds(dem, transform, crs, pixel_size_m, nodata,
                            route_utm_line, route_lonlat, distances,
                            max_radius_m, target_height_m, out_path):
    """
    Compute a viewshed at every sample distance and write them all into one
    GeoJSON FeatureCollection: one Feature per sample (polygon + distance/
    elevation/position properties) plus one Feature for the route itself
    (a constant LineString, for the viewer to draw as background).
    """
    features = []

    for i, distance in enumerate(distances):
        obs = observer_from_distance(dem, transform, crs, nodata, route_utm_line, distance)
        if obs is None:
            continue

        print(f"[{i + 1}/{len(distances)}] mile {distance * MILES_PER_METER:.2f}: computing viewshed...")
        visibility = compute_viewshed(
            dem, transform, pixel_size_m,
            obs["row"], obs["col"], obs["observer_z"],
            max_radius_m, target_height_m, nodata,
        )
        geometry = polygonize_visibility(visibility, transform, crs)

        features.append({
            "type": "Feature",
            "properties": {
                "index": i,
                "distance_m": obs["distance_m"],
                "distance_mi": obs["distance_m"] * MILES_PER_METER,
                "elevation_m": float(obs["ground_z"]),
                "lon": obs["lon"],
                "lat": obs["lat"],
            },
            "geometry": geometry,
        })

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


# ---------------------------------------------------------------------------
# 4. CLI entry point
# ---------------------------------------------------------------------------

def run_bbox(args):
    route_lonlat = load_route_points(args.gpx)
    bbox = compute_required_bbox(route_lonlat, args.max_radius, args.utm_crs)

    print(f"bbox: {bbox['min_lon']},{bbox['min_lat']},{bbox['max_lon']},{bbox['max_lat']}")
    print(f"estimated size: {bbox['est_width_px']} x {bbox['est_height_px']} px")
    if bbox["est_width_px"] > 2048 or bbox["est_height_px"] > 2048:
        print("WARNING: estimated size exceeds the ~2048x2048px single-request ceiling "
              "found for the USGS 3DEP ImageServer. This route may need a smaller "
              "--max-radius, or mosaicking multiple DEM tiles (not implemented).")
    print()
    print('--data-urlencode "bbox=' + f"{bbox['min_lon']},{bbox['min_lat']},"
          f"{bbox['max_lon']},{bbox['max_lat']}\"")


def run_export(args):
    dem, transform, crs, pixel_size_m, nodata = load_dem(args.dem)
    route_lonlat = load_route_points(args.gpx)
    line = route_to_utm_linestring(route_lonlat, crs)
    distances = sample_distances(line, args.step)

    estimate_runtime(len(distances))
    export_route_viewsheds(
        dem, transform, crs, pixel_size_m, nodata,
        line, route_lonlat, distances,
        args.max_radius, TARGET_HEIGHT_M, args.out,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bbox_parser = subparsers.add_parser("bbox", help="print the DEM bbox needed for a route")
    bbox_parser.add_argument("--gpx", default=GPX_PATH)
    bbox_parser.add_argument("--max-radius", type=float, default=MAX_RADIUS_M)
    bbox_parser.add_argument("--utm-crs", default=UTM_CRS,
                              help="UTM zone EPSG code for the route's location, e.g. EPSG:32610 "
                                   "for Tahoe vs EPSG:32611 for the Sierra default. Get this wrong "
                                   "and the bbox will be silently distorted, worse the further the "
                                   "route is from the zone's central meridian.")
    bbox_parser.set_defaults(func=run_bbox)

    export_parser = subparsers.add_parser("export", help="compute viewsheds along a route")
    export_parser.add_argument("--gpx", default=GPX_PATH)
    export_parser.add_argument("--dem", default=DEM_PATH)
    export_parser.add_argument("--out", default=OUTPUT_PATH)
    export_parser.add_argument("--step", type=float, default=STEP_DISTANCE_M)
    export_parser.add_argument("--max-radius", type=float, default=MAX_RADIUS_M)
    export_parser.set_defaults(func=run_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
