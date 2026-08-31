"""
Named terrain features (peaks, lakes) near a bbox.

Peaks: USGS GNIS, via the same Esri REST MapServer family already used
elsewhere in this project for elevation -- confirmed reliable for named
summits (e.g. "Twin Peaks" found correctly near Lake Tahoe).

Lakes: OpenStreetMap, via the Overpass API. GNIS's own Hydro Points layer
was tested during implementation and found to be missing major named
lakes entirely -- Lake Tahoe does not appear in ANY layer of the GNIS
geonames service for a bbox that clearly contains it, despite being an
official, well-known name. This isn't a rejection of GNIS for peaks
(that lookup is confirmed solid) -- it's a real, tested gap specific to
large lakes in that particular endpoint. OSM's natural=water tagging
includes Lake Tahoe correctly, so lakes use that source instead.
"""

import requests

GNIS_BASE_URL = "https://cartowfs.nationalmap.gov/arcgis/rest/services/geonames/MapServer"
PEAK_LAYER_ID = 2
PEAK_WHERE = "gaz_featureclass='Summit'"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_USER_AGENT = "caltopo-viewshed (personal portfolio project)"


def _fetch_gnis_peaks(bbox, timeout_s=30):
    """
    Query GNIS layer 2 (Landform, Summit features) for named peaks
    intersecting bbox (min_lon, min_lat, max_lon, max_lat) in EPSG:4326.

    Returns [{"name", "type": "peak", "lon", "lat"}, ...]. Raises
    RuntimeError on network failure, timeout, or an unparseable/error
    response, mirroring dem_fetch.fetch_dem_bytes's error-handling shape.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    params = {
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "gaz_name",
        "where": PEAK_WHERE,
        "f": "json",
    }
    url = f"{GNIS_BASE_URL}/{PEAK_LAYER_ID}/query"
    try:
        resp = requests.get(url, params=params, timeout=timeout_s)
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        raise RuntimeError(f"GNIS peaks query failed: {e}") from e

    if "error" in data:
        raise RuntimeError(f"GNIS peaks query returned an error: {data['error']}")

    peaks = []
    for f in data.get("features", []):
        geometry = f.get("geometry")
        if not geometry or not geometry.get("points"):
            continue
        lon, lat = geometry["points"][0]
        peaks.append({"name": f["attributes"]["gaz_name"], "type": "peak", "lon": lon, "lat": lat})
    return peaks


def _fetch_osm_lakes(bbox, timeout_s=30):
    """
    Query the Overpass API for named natural=water ways/relations
    intersecting bbox (min_lon, min_lat, max_lon, max_lat) in EPSG:4326.

    Uses `out center` to get one representative point per lake, since
    OSM water bodies are polygons (ways/relations), not points -- point-
    in-polygon testing against a viewshed only needs one representative
    point per lake, same as GNIS's peak points.

    Deduplicates by name: OSM sometimes tags both a multipolygon relation
    and its member way(s) with the same name for one real lake, which
    would otherwise show up twice.

    Returns [{"name", "type": "lake", "lon", "lat"}, ...]. Raises
    RuntimeError on network failure, timeout, or an unparseable response.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    query = f"""
    [out:json][timeout:{timeout_s}];
    (
      way[natural=water][name]({min_lat},{min_lon},{max_lat},{max_lon});
      relation[natural=water][name]({min_lat},{min_lon},{max_lat},{max_lon});
    );
    out center tags;
    """
    try:
        resp = requests.post(
            OVERPASS_URL, data={"data": query},
            headers={"User-Agent": OVERPASS_USER_AGENT}, timeout=timeout_s,
        )
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        raise RuntimeError(f"OSM lakes query failed: {e}") from e

    seen_names = set()
    lakes = []
    for el in data.get("elements", []):
        name = el.get("tags", {}).get("name")
        center = el.get("center")
        if not name or not center or name in seen_names:
            continue
        seen_names.add(name)
        lakes.append({"name": name, "type": "lake", "lon": center["lon"], "lat": center["lat"]})

    return lakes


def fetch_named_features(bbox, timeout_s=30):
    """
    Fetch named peaks (GNIS) and lakes (OSM/Overpass) intersecting bbox
    (min_lon, min_lat, max_lon, max_lat) in EPSG:4326.

    Each source's failure is caught independently (an Overpass timeout
    shouldn't discard peaks that already succeeded, and vice versa) --
    printed as a warning, contributing an empty list for that source
    only. Only if BOTH sources fail does this raise RuntimeError.

    Note: Esri /query responses can be capped (maxRecordCount, commonly
    1000) with no check for exceededTransferLimit here -- unlikely to
    matter for one route's bbox, a known v1 limitation.

    Returns a unified list of dicts: {"name", "type": "peak"|"lake",
    "lon", "lat"}.
    """
    results = []
    both_failed = True
    for fetch_fn, label in ((_fetch_gnis_peaks, "peaks"), (_fetch_osm_lakes, "lakes")):
        try:
            results.extend(fetch_fn(bbox, timeout_s))
            both_failed = False
        except RuntimeError as e:
            print(f"  warning: {label} lookup failed ({e})")

    if both_failed:
        raise RuntimeError("Named feature lookup failed for both peaks and lakes")

    return results
