# CalTopo Viewshed, From Scratch + Route Viewshed Animation App

[CalTopo](https://caltopo.com) already has a built-in custom viewshed layer — this project isn't trying to replace it. I wanted to understand how one actually works, so I rebuilt the feature myself: fetch real elevation data, implement the visibility algorithm from scratch (no GIS viewshed libraries), and export the result as a layer that imports directly into CalTopo.

A **viewshed** is the set of terrain visible from a given point, accounting for the ground blocking your view of anything behind it.

## Validation

The real test: does this independently-written algorithm agree with CalTopo's own built-in tool for the same point? I generated a viewshed with this code, imported it into CalTopo as an overlay (red outline), then turned on CalTopo's native viewshed for the same observer point (blue fill) on top of it.

<img src="images/caltopo_validation.jpg" alt="CalTopo built-in viewshed (purple) overlaid with this project's output (red outline), showing near-exact agreement" width="250">

The two agree almost exactly.

## How it works

1. **Elevation data** — a DEM patch is pulled from the [USGS 3DEP dynamic elevation service](https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer) (an ArcGIS ImageServer `exportImage` endpoint), centered on the observer point, then reprojected from EPSG:4326 into a local UTM zone so distances are true meters rather than degrees.
2. **Visibility algorithm** — a **rotational horizon sweep**: cast rays outward from the observer in every direction, walking each ray in one-pixel steps. Along each ray, track the steepest elevation angle seen so far; a point is visible only if it exceeds every angle seen closer in on that same ray. This is the standard trick that turns viewshed computation from an expensive per-cell ray-cast into a single pass per direction — see [`viewshed.py`](viewshed.py) for the full implementation. Since every ray is independent, they're dispatched across a `multiprocessing.Pool` rather than a plain loop — a ~2.7x speedup on an 8-core machine (sub-linear due to pool/IPC overhead), with the per-ray algorithm itself completely unchanged.
3. **Earth curvature + atmospheric refraction** — folded into one effective-Earth-radius correction, subtracted from a target's apparent height before computing its angle. Matters more than you'd expect at the several-kilometer scale mountain viewsheds operate at.
4. **Output** — the boolean visibility grid is polygonized, simplified, reprojected back to EPSG:4326, and written out as GeoJSON, which CalTopo imports directly as a map overlay.

## Results

**Mount Whitney summit** — a true summit gives the classic viewshed shape: a wide open fan toward the Owens Valley (nothing nearby to block it), plus thin sightlines along the crest to other peaks poking above the ridgeline.

<img src="images/whitney_summit_viewshed.jpg" alt="Fan-shaped viewshed from Mount Whitney's summit" width="350">

**A mid-slope point** — same algorithm, a different kind of location, and a much smaller result:

<img src="images/test_point_viewshed.jpg" alt="Small, irregular viewshed from a point partway down a slope" width="350">

Although close to the peak of Mt. Whitney, this point near trail camp has a lot less visible terrain. A small terrain bump close to the observer can cast an almost perfectly flat sightline that dominates the horizon for the rest of that ray, hiding everything behind it even if the ground drops thousands of feet further out. I confirmed this by tracing the raw per-pixel elevation profile in several directions from the point:

<img src="images/terrain_profile_debug.jpg" alt="Raw terrain elevation profile in multiple directions from the observer, showing a nearby high point dominating the horizon" width="500">

Summits get the expansive views; points partway down a slope often don't, even at high absolute elevation — the algorithm is just reporting what's actually true about that piece of terrain.

## Running it

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The DEM GeoTIFF isn't checked in (it's ~16MB and easy to regenerate). Fetch a patch centered on your observer point with something like:

```
curl -G "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage" \
  --data-urlencode "bbox=<minLon>,<minLat>,<maxLon>,<maxLat>" \
  --data-urlencode "bboxSR=4326" \
  --data-urlencode "size=2048,2048" \
  --data-urlencode "imageSR=4326" \
  --data-urlencode "format=tiff" \
  --data-urlencode "pixelType=F32" \
  --data-urlencode "noData=-9999" \
  --data-urlencode "noDataInterpretation=esriNoDataMatchAny" \
  --data-urlencode "interpolation=RSP_BilinearInterpolation" \
  --data-urlencode "f=image" \
  -o dem.tif
```

Reproject it to a local UTM zone (needed so ray distances are true meters), point `DEM_PATH`/`OBSERVER_LON`/`OBSERVER_LAT` in [`viewshed.py`](viewshed.py) at your data and point, then:

```
python3 viewshed.py
```

This writes a debug PNG (DEM + visibility overlay) and a `.geojson` file ready to import into CalTopo.

### Using a different location

Nothing about the algorithm is specific to Mount Whitney — it operates on whatever DEM array, affine transform, and CRS it's handed. To point it somewhere else:

- Fetch a new DEM patch centered on the new observer point (same `exportImage` call, new `bbox`)
- Reproject to the correct UTM zone for that longitude — this project hardcodes `EPSG:32611` (UTM 11N), which is only correct for the Sierra Nevada
- Update `DEM_PATH`, `OBSERVER_LON`, and `OBSERVER_LAT` in `viewshed.py`
- Make sure `MAX_RADIUS_M` still fits inside the fetched DEM's extent, so rays don't run off the edge

One real constraint: **USGS 3DEP only covers the United States.** Outside the US, swap in a global DEM source instead (e.g. Copernicus GLO-30 or SRTM) — the algorithm doesn't care where the elevation data came from, only that it gets an array, transform, and CRS in the same shape.

## Route viewshed: interactive scrubber

Building on the single-point tool, [`route_animation.py`](route_animation.py) samples a hiking route at fixed distance intervals (0.5 miles by default) and computes a full viewshed at every sample point — same algorithm, same validation, just run repeatedly along a trail. Rather than a passive animation, the output drives a small interactive page: drag a slider and watch the visible area change as the position moves along the route, similar to CalTopo's own elevation-profile scrubber.

**[Try it live](https://jacobburrill11.github.io/caltopo-viewshed/route_viewshed_viewer.html)** — the demo is a real ~1.5 mile trail near Lake Tahoe ([`examples/cinder_cone_trail.gpx`](examples/cinder_cone_trail.gpx)), sampled every 0.25 miles.

How it works:

1. Export the route as GPX from wherever you tracked it — CalTopo, Strava, AllTrails, Gaia GPS, and Garmin Connect all support this. GPX (not GeoJSON) is the format these apps actually hand you.
2. `python3 route_animation.py export --gpx route.gpx --out route_viewsheds.geojson` computes a viewshed at every sample point and writes them all into one GeoJSON `FeatureCollection`. Everything is automatic — UTM zone is auto-detected from the route's coordinates (override with `--utm-crs` only if you need to force a zone), and each sample point fetches its own small DEM tile (sized to the search radius, discarded after use) rather than one DEM for the whole route. That means **route length has no bearing on whether this succeeds** — a 4-mile route and a 40-mile route both work the same way, just with proportionally more sample points and runtime (roughly 10-15s per sample, fetch + compute). Add `--eye-height METERS` to override the default 1.7m (eye level for a standing person) — useful for a fire lookout, a tall viewpoint, or just padding out GPS/coordinate imprecision, though it's an explicit override rather than the validated default.
3. Open [`route_viewshed_viewer.html`](route_viewshed_viewer.html) — needs a local server for its `fetch()` call to work (e.g. `python3 -m http.server`), or just visit the hosted version linked above.

The basemap imagery in the viewer is fetched live by the browser (Esri World Imagery tiles), not generated by this project — the only thing this pipeline produces is the GeoJSON data. Every individual sample's polygon is also independently CalTopo-importable, exactly like the single-point workflow — the interactive viewer and CalTopo import both read the same output file.

Elevation always comes from the DEM, never from the GPX file's own elevation tags — GPX elevation is frequently absent, barometric, or noisy, and mixing it in would undermine the curvature-corrected geometry already validated against CalTopo.

## Web app

[`webapp/`](webapp/) is a small Flask app wrapping the same automatic pipeline as the CLI above — upload a GPX, and DEM fetch (`dem_fetch.py`), UTM zone detection, reprojection, and viewshed export all happen per sample point, landing on the same interactive scrubber view.

```
source .venv/bin/activate
python3 webapp/app.py
```

This opens your browser to `http://127.0.0.1:5000` automatically. The dashboard lists any routes you've already computed; "Upload new route" takes you to the form (eye height defaults to 10m here — deliberately more generous than the CLI's validated 1.7m, to allow room for GPS/coordinate imprecision; step distance defaults to 0.5mi, and you can give the route a label). Computation can take a while depending on route length (roughly 10-15s per sample point, fetch + compute) — no route length triggers a failure, just more sample points to work through. Each computed route is saved (`route_store.py`) so you can come back and view it again later, or delete it from the dashboard.

This is still an early step: single-user with no accounts, synchronous request/response (no progress bar, just a "please wait" message). GIF export is planned as a later step — not built yet.

## Named peaks and lakes

Every sample point's viewshed also gets checked against nearby named terrain features: **peaks** from [USGS GNIS](https://www.usgs.gov/us-board-on-geographic-names) and **lakes** from OpenStreetMap ([`terrain_features.py`](terrain_features.py)). One query covers the whole route (not one per sample), then each sample's already-computed polygon gets a fast local point-in-polygon check against the shared candidate list.

The two-source split exists for a real, tested reason: GNIS's own point-feature layer for lakes is missing major named lakes entirely — Lake Tahoe doesn't appear in it at all for a bbox that clearly contains it, despite being an official, authoritative name. GNIS's peak data is solid (verified against real named summits), so peaks stay on GNIS; lakes come from OpenStreetMap instead, which does have Lake Tahoe correctly. A lookup failure on either source degrades gracefully — the viewshed computation itself always completes, just with fewer (or zero) labeled features for that run.

In the viewer, labeled markers on the map update as you drag the scrubber (only what's visible from that exact point); a separate collapsible panel lists every named feature visible from anywhere along the whole route.

## Ideas for extending this

- **A "view quality" score** — beyond just visible/not-visible, rank viewpoints by something like the number of named peaks visible, number of visible bodies of water, or the ruggedness/steepness of the visible terrain.

## Why

I'm building a portfolio of projects while looking for data analyst/data scientist roles. This one covers real GIS data handling, an algorithm implemented from its mathematical description rather than pulled from a library, and end-to-end validation against a real production tool.
