"""
Viewshed generator for CalTopo import.

Computes which terrain is visible from a given observer point using a
rotational horizon-sweep algorithm, and exports the result as a GeoJSON
polygon suitable for importing into CalTopo as a map overlay.

Sections below are grouped as:
  1. Constants / parameters
  2. Geo I/O (loading the DEM, coordinate conversions)
  3. Core algorithm (bilinear interpolation, curvature/refraction, the rotational horizon sweep)
  4. Output conversion (raster mask -> GeoJSON)
  5. Debug visualization
  6. Script entry point
"""

import json
import math
import multiprocessing
from datetime import date

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform
from rasterio.features import shapes as raster_shapes
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# 1. Constants / parameters
# ---------------------------------------------------------------------------

DEM_PATH = "2026-08-25_whitney-viewshed_dem.tif"

OBSERVER_LON = -118.27849
OBSERVER_LAT = 36.56334

EYE_HEIGHT_M = 1.7      # observer height offset above ground
TARGET_HEIGHT_M = 0.0   # target height offset above ground (0 = bare-ground visibility)

MAX_RADIUS_M = 14000.0  # stay a bit inside the DEM's ~15km/~19km half-extents

EARTH_RADIUS_M = 6_371_000.0
REFRACTION_COEFFICIENT = 0.13
EFFECTIVE_RADIUS_M = EARTH_RADIUS_M / (1 - REFRACTION_COEFFICIENT)

SIMPLIFY_TOLERANCE_M = 10.0  # for cleaning up the exported polygon


# ---------------------------------------------------------------------------
# 2. Geo I/O
# ---------------------------------------------------------------------------

def load_dem(path):
    """
    Open the DEM GeoTIFF and return everything downstream code needs:
    (elevation_array, transform, crs, pixel_size_m, nodata_value)
    """
    with rasterio.open(path) as src:
        elevation = src.read(1)
        transform = src.transform
        crs = src.crs
        pixel_size_m = src.res[0]  # square pixels, so res[0] == res[1]
        nodata = src.nodata if src.nodata is not None else -9999.0
    return elevation, transform, crs, pixel_size_m, nodata


def lonlat_to_pixel(lon, lat, transform, crs):
    """
    Convert a geographic (EPSG:4326) lon/lat into a fractional (row, col)
    position in the raster. Reprojects into the raster's CRS first.
    """
    xs, ys = warp_transform("EPSG:4326", crs, [lon], [lat])
    col, row = ~transform * (xs[0], ys[0])
    return row, col


def pixel_to_coords(row, col, transform):
    """Convert a (fractional) pixel row/col into raster-CRS (x, y) meters."""
    x, y = transform * (col, row)
    return x, y


def coords_to_pixel(x, y, transform):
    """Inverse of pixel_to_coords: raster-CRS (x, y) meters into fractional (row, col)."""
    col, row = ~transform * (x, y)
    return row, col


# ---------------------------------------------------------------------------
# 3. Core algorithm -- rotational horizon sweep
# ---------------------------------------------------------------------------

def bilinear_interpolate(dem, row, col, nodata):
    """
    Interpolate the elevation at a fractional (row, col) position from the
    four surrounding DEM cells.

    Returns the interpolated elevation as a float, or None if (row, col)
    falls outside the array bounds or any of the four surrounding cells
    equals `nodata`. Callers treat a None return as "blocked" (opaque).
    """
    row0, col0 = int(math.floor(row)), int(math.floor(col))
    row1, col1 = row0 + 1, col0 + 1
    top_left = dem[row0, col0] if 0 <= row0 < dem.shape[0] and 0 <= col0 < dem.shape[1] else None
    top_right = dem[row0, col1] if 0 <= row0 < dem.shape[0] and 0 <= col1 < dem.shape[1] else None
    bottom_left = dem[row1, col0] if 0 <= row1 < dem.shape[0] and 0 <= col0 < dem.shape[1] else None
    bottom_right = dem[row1, col1] if 0 <= row1 < dem.shape[0] and 0 <= col1 < dem.shape[1] else None
    if top_left == nodata or bottom_left == nodata or top_right == nodata or bottom_right == nodata:
        return None
    frac_row = row - row0
    frac_col = col - col0
    top_value = top_left+frac_col * (top_right - top_left) if top_left is not None and top_right is not None else None
    bottom_value = bottom_left+frac_col * (bottom_right - bottom_left) if bottom_left is not None and bottom_right is not None else None
    if top_value is None or bottom_value is None:
        return None
    return top_value + frac_row * (bottom_value - top_value)


def curvature_drop(distance_m, effective_radius_m=EFFECTIVE_RADIUS_M):
    """
    Return the curvature + refraction drop, in meters, for a point at
    `distance_m` from the observer: distance squared, divided by (2 times
    the effective radius).
    """
    return (distance_m ** 2) / (2 * effective_radius_m)


def sweep_ray(dem, transform, observer_row, observer_col, observer_z,
              theta, pixel_size_m, max_radius_m, target_height_m, nodata):
    """
    Walk outward from the observer along direction `theta` (radians, measured
    in the raster's CRS plane), in pixel_size_m increments, out to
    max_radius_m, tracking the running horizon angle. A sample is visible if
    its elevation angle from the observer is greater than or equal to the
    steepest angle seen so far along this ray; a nodata/out-of-bounds sample
    is treated as blocked (skipped, without updating the horizon).

    Returns a list of (row, col) integer pixel positions found visible along
    this ray.
    """
    observer_x, observer_y = pixel_to_coords(observer_row, observer_col, transform) 
    visible_positions = []
    steepest_angle_so_far = -math.inf
    for d in np.arange(pixel_size_m, max_radius_m + pixel_size_m, pixel_size_m):
        # Calculate the new position in meters
        new_x = observer_x + d * math.cos(theta)
        new_y = observer_y + d * math.sin(theta)
        
        # Convert back to pixel coordinates
        new_row, new_col = coords_to_pixel(new_x, new_y, transform)
        
        # Interpolate elevation at the new position
        elevation = bilinear_interpolate(dem, new_row, new_col, nodata)
        
        if elevation is None:
            continue  # Skip this point if it's out of bounds or nodata
        
        # Calculate apparent height difference
        apparent_height_diff = (elevation + target_height_m) - observer_z - curvature_drop(d)
        
        # Calculate angle
        angle = math.atan2(apparent_height_diff, d)
        
        if angle >= steepest_angle_so_far:
            visible_positions.append((int(round(new_row)), int(round(new_col))))
            steepest_angle_so_far = angle
    
    return visible_positions


_worker_args = None  # set once per worker process by _init_worker


def _init_worker(dem, transform, observer_row, observer_col, observer_z,
                  pixel_size_m, max_radius_m, target_height_m, nodata):
    """
    Pool initializer: runs once per worker process (not once per ray), so
    the DEM and other shared arguments are pickled to each worker a single
    time rather than re-sent for every angle.
    """
    global _worker_args
    _worker_args = (dem, transform, observer_row, observer_col, observer_z,
                     pixel_size_m, max_radius_m, target_height_m, nodata)


def _sweep_ray_worker(theta):
    """Call sweep_ray, unchanged, inside a worker process for one angle."""
    dem, transform, observer_row, observer_col, observer_z, \
        pixel_size_m, max_radius_m, target_height_m, nodata = _worker_args
    return sweep_ray(dem, transform, observer_row, observer_col, observer_z,
                      theta, pixel_size_m, max_radius_m, target_height_m, nodata)


def compute_viewshed(dem, transform, pixel_size_m, observer_row, observer_col,
                      observer_z, max_radius_m, target_height_m, nodata,
                      num_workers=None):
    """
    Orchestrate the full sweep: choose an angular step (~pixel_size_m /
    max_radius_m radians, so adjacent rays are about one pixel apart at max
    range), call sweep_ray once per angle around the full circle, and OR
    every ray's visible cells into one boolean array the same shape as
    `dem`. The observer's own cell is marked visible directly.

    Rays are independent of each other, so they're dispatched across a
    multiprocessing.Pool (num_workers defaults to the machine's CPU count)
    instead of a plain Python loop -- sweep_ray's own logic is untouched,
    this only parallelizes how many times it gets called at once.

    Returns the boolean visibility array.
    """
    angular_step = pixel_size_m / max_radius_m
    num_steps = int(math.ceil(2 * math.pi / angular_step))
    thetas = [step * angular_step for step in range(num_steps)]

    visibility = np.zeros(dem.shape, dtype=bool)

    with multiprocessing.Pool(
        processes=num_workers,
        initializer=_init_worker,
        initargs=(dem, transform, observer_row, observer_col, observer_z,
                  pixel_size_m, max_radius_m, target_height_m, nodata),
    ) as pool:
        for visible_positions in pool.imap_unordered(_sweep_ray_worker, thetas, chunksize=32):
            for row, col in visible_positions:
                if 0 <= row < dem.shape[0] and 0 <= col < dem.shape[1]:
                    visibility[row, col] = True

    visibility[observer_row, observer_col] = True  # Mark observer's own cell as visible
    return visibility


# ---------------------------------------------------------------------------
# 4. Output conversion
# ---------------------------------------------------------------------------

def polygonize_visibility(visibility, transform, crs, simplify_tolerance_m=SIMPLIFY_TOLERANCE_M):
    """
    Convert a boolean visibility array into a single simplified polygon
    (or multipolygon) in EPSG:4326, ready to drop into a GeoJSON feature.
    """
    mask = visibility.astype(np.uint8)
    geoms = [
        shape(geom)
        for geom, value in raster_shapes(mask, mask=visibility, transform=transform)
        if value == 1
    ]
    if not geoms:
        raise ValueError("No visible cells found -- nothing to polygonize")

    merged = unary_union(geoms).simplify(simplify_tolerance_m)

    from rasterio.warp import transform_geom
    reprojected = transform_geom(crs, "EPSG:4326", mapping(merged))
    return reprojected


def save_geojson(geometry, path, properties=None):
    """Wrap a GeoJSON geometry dict in a FeatureCollection and write it to disk."""
    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": properties or {},
                "geometry": geometry,
            }
        ],
    }
    with open(path, "w") as f:
        json.dump(feature_collection, f)


# ---------------------------------------------------------------------------
# 5. Debug visualization
# ---------------------------------------------------------------------------

def plot_visibility(dem, visibility, observer_row, observer_col, out_path=None):
    """Quick sanity-check plot: DEM in grayscale, visible area shaded, observer marked."""
    fig, ax = plt.subplots(figsize=(9, 9))
    ax.imshow(dem, cmap="gray")
    overlay = np.ma.masked_where(~visibility, visibility)
    ax.imshow(overlay, cmap="autumn", alpha=0.5)
    ax.plot(observer_col, observer_row, "b*", markersize=15, label="Observer")
    ax.legend()
    ax.set_title("Viewshed from observer point")
    if out_path:
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
    else:
        plt.show()


# ---------------------------------------------------------------------------
# 6. Script entry point
# ---------------------------------------------------------------------------

def main():
    dem, transform, crs, pixel_size_m, nodata = load_dem(DEM_PATH)

    obs_row_f, obs_col_f = lonlat_to_pixel(OBSERVER_LON, OBSERVER_LAT, transform, crs)
    obs_row, obs_col = int(round(obs_row_f)), int(round(obs_col_f))

    observer_ground_z = float(dem[obs_row, obs_col])
    observer_z = observer_ground_z + EYE_HEIGHT_M

    print(f"Observer pixel: ({obs_row}, {obs_col}), ground elevation: {observer_ground_z:.1f} m")

    visibility = compute_viewshed(
        dem, transform, pixel_size_m,
        obs_row, obs_col, observer_z,
        MAX_RADIUS_M, TARGET_HEIGHT_M, nodata,
    )

    valid_cells = np.count_nonzero(dem != nodata)
    visible_cells = np.count_nonzero(visibility)
    print(f"Visible: {visible_cells} / {valid_cells} valid cells "
          f"({100 * visible_cells / valid_cells:.1f}%)")

    today = date.today().isoformat()

    plot_visibility(dem, visibility, obs_row, obs_col,
                     out_path=f"{today}_whitney-viewshed_debug.png")

    geometry = polygonize_visibility(visibility, transform, crs)
    save_geojson(
        geometry,
        f"{today}_whitney-viewshed_viewshed.geojson",
        properties={
            "observer_lon": OBSERVER_LON,
            "observer_lat": OBSERVER_LAT,
            "eye_height_m": EYE_HEIGHT_M,
            "target_height_m": TARGET_HEIGHT_M,
            "max_radius_m": MAX_RADIUS_M,
        },
    )
    print(f"Wrote {today}_whitney-viewshed_viewshed.geojson")


if __name__ == "__main__":
    main()
