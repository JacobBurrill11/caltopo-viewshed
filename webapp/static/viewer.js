const map = L.map('map');

L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles &copy; Esri',
  maxZoom: 19,
}).addTo(map);

let viewshedLayer = null;
let marker = null;
let samples = []; // one entry per viewshed sample, sorted by index

function showSample(i) {
  const sample = samples[i];
  if (!sample) return;

  if (viewshedLayer) map.removeLayer(viewshedLayer);
  viewshedLayer = L.geoJSON(sample, {
    style: { color: '#ff3b3b', weight: 2, fillColor: '#ff3b3b', fillOpacity: 0.35 },
  }).addTo(map);

  const [lon, lat] = [sample.properties.lon, sample.properties.lat];
  if (marker) {
    marker.setLatLng([lat, lon]);
  } else {
    marker = L.circleMarker([lat, lon], { radius: 7, color: '#1e90ff', fillColor: '#1e90ff', fillOpacity: 1 }).addTo(map);
  }

  document.getElementById('readout').textContent =
    `Mile ${sample.properties.distance_mi.toFixed(2)} of ${samples[samples.length - 1].properties.distance_mi.toFixed(2)}` +
    ` — ${Math.round(sample.properties.elevation_m)} m elevation`;
}

fetch('/results/current/route_viewsheds.geojson')
  .then(r => r.json())
  .then(fc => {
    const routeFeature = fc.features.find(f => f.properties && f.properties.name === 'route');
    samples = fc.features
      .filter(f => f.properties && f.properties.index !== undefined)
      .sort((a, b) => a.properties.index - b.properties.index);

    if (routeFeature) {
      const routeLine = L.geoJSON(routeFeature, { style: { color: '#00e5ff', weight: 3 } }).addTo(map);
      map.fitBounds(routeLine.getBounds(), { padding: [30, 30] });
    } else if (samples.length) {
      map.setView([samples[0].properties.lat, samples[0].properties.lon], 13);
    }

    const slider = document.getElementById('slider');
    slider.max = samples.length - 1;
    slider.disabled = false;
    slider.addEventListener('input', () => showSample(parseInt(slider.value, 10)));

    document.getElementById('status').style.display = 'none';
    if (samples.length) showSample(0);
  })
  .catch(err => {
    document.getElementById('status').textContent =
      'Failed to load route_viewsheds.geojson: ' + err;
  });
