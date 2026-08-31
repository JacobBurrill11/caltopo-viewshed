"""
Automated DEM fetch + reprojection.

Replaces the manual steps documented in earlier versions of the README
(compute a bbox, curl the USGS 3DEP ImageServer, reproject with rasterio)
with callable Python functions, so both the CLI and the web app can run
the whole pipeline without a human in the loop. Kept at top level (not
inside webapp/) since it's generically reusable.

Every fetch requests a fixed 2048x2048px image from USGS regardless of
the geographic area covered -- a bigger area only ever means coarser
resolution (meters/pixel), never a failed request. compute_required_bbox
and fetch_and_reproject_dem's checks reflect that: they warn/fail based
on projected resolution, not on the pixel count a request would need to
maintain some fixed target resolution (that was never what got sent).
"""

import rasterio
import requests
from rasterio.io import MemoryFile
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform as warp_transform

USGS_EXPORT_IMAGE_URL = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
)

TIFF_MAGIC_PREFIXES = (b"II*\x00", b"MM\x00*")

DEM_REQUEST_SIZE_PX = 2048        # every fetch requests exactly this many pixels, regardless of area
WARN_RESOLUTION_M_PER_PX = 50.0   # ~5x USGS 3DEP's native ~10m -- still resolves real terrain features
FAIL_RESOLUTION_M_PER_PX = 500.0  # ~50x native (~512km radius) -- a typo/misconfiguration guard only,
                                   # not a real operating limit; max_radius_m is a curated constant/CLI
                                   # flag, never raw end-user input


def compute_required_bbox(route_lonlat, max_radius_m, utm_crs):
    """
    Compute the lon/lat bounding box needed to fetch a DEM covering every
    point in route_lonlat plus a max_radius_m buffer in every direction.

    Steps:
      - Reproject route_lonlat into utm_crs.
      - Take the min/max x and min/max y of the projected points, then
        buffer those bounds outward by max_radius_m on every side.
      - Reproject the 4 buffered corners (not just the center point) back
        to EPSG:4326 and take the min/max lon/lat of the results -- UTM
        axes aren't lon/lat-aligned, so buffering in one CRS and taking a
        naive min/max in the other would clip corners.
      - Compute the resulting resolution (bbox width / DEM_REQUEST_SIZE_PX,
        since every fetch requests that many pixels regardless of area),
        and print a note if it's coarser than WARN_RESOLUTION_M_PER_PX.

    Works equally well for a single point (route_lonlat of length 1) --
    the per-sample viewshed pipeline relies on exactly this, producing a
    bbox that's just that point buffered by max_radius_m on every side.

    Returns a dict: {"min_lon", "min_lat", "max_lon", "max_lat",
    "resolution_m_per_px"}.
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

    resolution_m_per_px = (max_x - min_x) / DEM_REQUEST_SIZE_PX
    if resolution_m_per_px > WARN_RESOLUTION_M_PER_PX:
        print(f"Note: this fetch resolves to ~{resolution_m_per_px:.0f} m/pixel "
              f"(USGS 3DEP native is ~10m) -- consider a smaller max_radius for more detail.")

    return {
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "resolution_m_per_px": resolution_m_per_px,
    }


def fetch_dem_bytes(bbox, size_px=(DEM_REQUEST_SIZE_PX, DEM_REQUEST_SIZE_PX), timeout_s=120):
    """
    Fetch a DEM GeoTIFF from the USGS 3DEP ImageServer for the given
    (min_lon, min_lat, max_lon, max_lat) bbox.

    Returns the raw response bytes. Raises RuntimeError with a readable
    message if the service is unreachable, times out, or returns
    something that isn't real TIFF bytes -- the ImageServer can return
    HTTP 200 with a JSON error body instead of image data on malformed
    parameters, so that case is checked for explicitly rather than
    letting rasterio fail later with an opaque error.
    """
    params = {
        "bbox": ",".join(str(v) for v in bbox),
        "bboxSR": "4326",
        "size": f"{size_px[0]},{size_px[1]}",
        "imageSR": "4326",
        "format": "tiff",
        "pixelType": "F32",
        "noData": "-9999",
        "noDataInterpretation": "esriNoDataMatchAny",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }
    try:
        resp = requests.get(USGS_EXPORT_IMAGE_URL, params=params, timeout=timeout_s)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"USGS elevation service unreachable or timed out: {e}") from e

    content = resp.content
    if not content.startswith(TIFF_MAGIC_PREFIXES):
        try:
            detail = resp.json()
        except ValueError:
            detail = content[:300]
        raise RuntimeError(f"USGS elevation service did not return a DEM: {detail}")

    return content


def reproject_dem_to_utm(raw_tif_bytes, utm_crs, out_path):
    """
    Reproject raw DEM bytes (as returned by fetch_dem_bytes, in EPSG:4326)
    into utm_crs and write the result to out_path.
    """
    with MemoryFile(raw_tif_bytes) as memfile, memfile.open() as src:
        transform, width, height = calculate_default_transform(
            src.crs, utm_crs, src.width, src.height, *src.bounds)
        kwargs = src.meta.copy()
        kwargs.update({"crs": utm_crs, "transform": transform, "width": width, "height": height})

        with rasterio.open(out_path, "w", **kwargs) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=transform, dst_crs=utm_crs,
                resampling=Resampling.bilinear,
            )


def fetch_and_reproject_dem(route_lonlat, max_radius_m, utm_crs, out_path):
    """
    Orchestrate the full automated DEM acquisition: compute the required
    bbox, fetch it from USGS 3DEP, and reproject it into utm_crs, writing
    the result to out_path.

    Raises ValueError before making any network request only if the
    resulting resolution would be coarser than FAIL_RESOLUTION_M_PER_PX --
    a defensive guard against a wildly misconfigured max_radius_m (e.g. a
    typo adding extra zeros), not a real operating limit. Ordinary values,
    including large ones, resolve fine; this never fires at the project's
    actual defaults.

    Returns the bbox dict from compute_required_bbox, for logging.
    """
    bbox = compute_required_bbox(route_lonlat, max_radius_m, utm_crs)
    if bbox["resolution_m_per_px"] > FAIL_RESOLUTION_M_PER_PX:
        raise ValueError(
            f"max_radius_m={max_radius_m:.0f} resolves to ~{bbox['resolution_m_per_px']:.0f} "
            f"m/pixel -- too coarse to be meaningful. Check max_radius_m for a typo."
        )

    raw_bytes = fetch_dem_bytes((bbox["min_lon"], bbox["min_lat"], bbox["max_lon"], bbox["max_lat"]))
    reproject_dem_to_utm(raw_bytes, utm_crs, out_path)
    return bbox
