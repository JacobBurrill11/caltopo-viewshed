# CalTopo Viewshed, From Scratch

[CalTopo](https://caltopo.com) already has a built-in viewshed layer — this project isn't trying to replace it. I wanted to understand how one actually works, so I rebuilt the feature myself: fetch real elevation data, implement the visibility algorithm from scratch (no GIS viewshed libraries), and export the result as a layer that imports directly into CalTopo.

A **viewshed** is the set of terrain visible from a given point, accounting for the ground blocking your view of anything behind it.

## Validation

The real test: does this independently-written algorithm agree with CalTopo's own built-in tool for the same point? I generated a viewshed with this code, imported it into CalTopo as an overlay (red outline), then turned on CalTopo's native viewshed for the same observer point (purple fill) on top of it.

<img src="images/caltopo_validation.jpg" alt="CalTopo built-in viewshed (purple) overlaid with this project's output (red outline), showing near-exact agreement" width="350">

The two agree almost exactly.

## How it works

1. **Elevation data** — a DEM patch is pulled from the [USGS 3DEP dynamic elevation service](https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer) (an ArcGIS ImageServer `exportImage` endpoint), centered on the observer point, then reprojected from EPSG:4326 into a local UTM zone so distances are true meters rather than degrees.
2. **Visibility algorithm** — a **rotational horizon sweep**: cast rays outward from the observer in every direction, walking each ray in one-pixel steps. Along each ray, track the steepest elevation angle seen so far; a point is visible only if it exceeds every angle seen closer in on that same ray. This is the standard trick that turns viewshed computation from an expensive per-cell ray-cast into a single pass per direction — see [`viewshed.py`](viewshed.py) for the full implementation.
3. **Earth curvature + atmospheric refraction** — folded into one effective-Earth-radius correction, subtracted from a target's apparent height before computing its angle. Matters more than you'd expect at the several-kilometer scale mountain viewsheds operate at.
4. **Output** — the boolean visibility grid is polygonized, simplified, reprojected back to EPSG:4326, and written out as GeoJSON, which CalTopo imports directly as a map overlay.

## Results

**Mount Whitney summit** — a true summit gives the classic viewshed shape: a wide open fan toward the Owens Valley (nothing nearby to block it), plus thin sightlines along the crest to other peaks poking above the ridgeline.

<img src="images/whitney_summit_viewshed.jpg" alt="Fan-shaped viewshed from Mount Whitney's summit" width="500">

**A mid-slope point** — same algorithm, a different kind of location, and a much smaller result:

<img src="images/test_point_viewshed.jpg" alt="Small, irregular viewshed from a point partway down a slope" width="500">

This isn't a bug — it's a real property of horizon-sweep viewsheds. A small terrain bump close to the observer can cast an almost perfectly flat sightline that dominates the horizon for the rest of that ray, hiding everything behind it even if the ground drops thousands of feet further out. I confirmed this by tracing the raw per-pixel elevation profile in several directions from the point:

<img src="images/terrain_profile_debug.jpg" alt="Raw terrain elevation profile in multiple directions from the observer, showing a nearby high point dominating the horizon" width="600">

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

## Ideas for extending this

- **Viewshed animation along a route** — given a hiking route (a line or polygon), generate an animation showing how the viewshed changes step by step along the trail.
- **A "view quality" score** — beyond just visible/not-visible, rank viewpoints by something like the number of named peaks visible, number of visible bodies of water, or the ruggedness/steepness of the visible terrain.

## Why

I'm building a portfolio of projects while looking for data analyst/data scientist roles. This one covers real GIS data handling, an algorithm implemented from its mathematical description rather than pulled from a library, and end-to-end validation against a real production tool.
