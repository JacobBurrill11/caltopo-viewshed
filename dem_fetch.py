"""
Automated DEM fetch + reprojection.

Replaces the three manual steps documented in the README (compute a bbox,
curl the USGS 3DEP ImageServer, reproject with rasterio) with callable
Python functions, so the web app can run the whole pipeline without a
human in the loop. Kept at top level (not inside webapp/) since it's
generically reusable -- e.g. by a future CLI --auto-fetch flag.
"""

import rasterio
import requests
from rasterio.io import MemoryFile
from rasterio.warp import Resampling, calculate_default_transform, reproject

from route_animation import compute_required_bbox

USGS_EXPORT_IMAGE_URL = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
)

TIFF_MAGIC_PREFIXES = (b"II*\x00", b"MM\x00*")


def fetch_dem_bytes(bbox, size_px=(2048, 2048), timeout_s=120):
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


def fetch_and_reproject_dem(route_lonlat, max_radius_m, utm_crs, out_path, pixel_ceiling=2048):
    """
    Orchestrate the full automated DEM acquisition for a route: compute
    the required bbox, fetch it from USGS 3DEP, and reproject it into
    utm_crs, writing the result to out_path.

    Raises ValueError before making any network request if the required
    bbox would exceed pixel_ceiling in either dimension -- the
    empirically found ceiling for a single USGS ImageServer request (no
    mosaicking is implemented), so failing fast here avoids burning a
    slow round trip on a request that would just error out anyway.

    Returns the bbox dict from compute_required_bbox, for logging.
    """
    bbox = compute_required_bbox(route_lonlat, max_radius_m, utm_crs)
    if bbox["est_width_px"] > pixel_ceiling or bbox["est_height_px"] > pixel_ceiling:
        raise ValueError(
            f"Route + search radius is too large for a single DEM fetch "
            f"({bbox['est_width_px']:.0f} x {bbox['est_height_px']:.0f} px, "
            f"ceiling is {pixel_ceiling}px). Try a shorter route."
        )

    raw_bytes = fetch_dem_bytes((bbox["min_lon"], bbox["min_lat"], bbox["max_lon"], bbox["max_lat"]))
    reproject_dem_to_utm(raw_bytes, utm_crs, out_path)
    return bbox
