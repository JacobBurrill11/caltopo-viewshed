const map = L.map('map');

L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles &copy; Esri',
  maxZoom: 19,
}).addTo(map);

const FEATURE_COLORS = { peak: '#f5f11c', lake: '#1752b8' };

let viewshedLayer = null;
let marker = null;
let featureLayer = null;
let samples = []; // one entry per viewshed sample, sorted by index

function showSample(i) {
  const sample = samples[i];
  if (!sample) return;

  if (viewshedLayer) map.removeLayer(viewshedLayer);
  viewshedLayer = L.geoJSON(sample, {
    style: { color: '#3bffde', weight: 2, fillColor: '#3bffde', fillOpacity: 0.35 },
  }).addTo(map);

  const [lon, lat] = [sample.properties.lon, sample.properties.lat];
  if (marker) {
    marker.setLatLng([lat, lon]);
  } else {
    marker = L.circleMarker([lat, lon], { radius: 7, color: '#ff1ee5', fillColor: '#ff1ee5', fillOpacity: 1 }).addTo(map);
  }

  if (featureLayer) map.removeLayer(featureLayer);
  const visibleFeatures = sample.properties.visible_features || [];
  featureLayer = L.layerGroup(
    visibleFeatures.map(f => L.circleMarker([f.lat, f.lon], {
      radius: 6,
      color: FEATURE_COLORS[f.type] || '#666',
      fillColor: FEATURE_COLORS[f.type] || '#666',
      fillOpacity: 0.9,
    }).bindTooltip(f.name))
  ).addTo(map);

  document.getElementById('readout').textContent =
    `Mile ${sample.properties.distance_mi.toFixed(2)} of ${samples[samples.length - 1].properties.distance_mi.toFixed(2)}` +
    ` — ${Math.round(sample.properties.elevation_m)} m elevation`;
}

fetch(`/results/${window.ROUTE_ID}/route_viewsheds.geojson`)
  .then(r => r.json())
  .then(fc => {
    const routeFeature = fc.features.find(f => f.properties && f.properties.name === 'route');
    samples = fc.features
      .filter(f => f.properties && f.properties.index !== undefined)
      .sort((a, b) => a.properties.index - b.properties.index);

    if (routeFeature) {
      const routeLine = L.geoJSON(routeFeature, { style: { color: '#ff1ee5', weight: 3 } }).addTo(map);
      map.fitBounds(routeLine.getBounds(), { padding: [30, 30] });
    } else if (samples.length) {
      map.setView([samples[0].properties.lat, samples[0].properties.lon], 13);
    }

    // Aggregate list: union-by-name of every sample's visible_features,
    // computed client-side since each sample already carries its own list.
    const aggregate = new Map(); // name -> {name, type, lon, lat}
    samples.forEach(s => (s.properties.visible_features || []).forEach(f => {
      if (!aggregate.has(f.name)) aggregate.set(f.name, f);
    }));
    const aggregateList = Array.from(aggregate.values()).sort((a, b) => a.name.localeCompare(b.name));

    const listEl = document.getElementById('feature-list');
    aggregateList.forEach(f => {
      const li = document.createElement('li');
      const dot = document.createElement('span');
      dot.style.color = FEATURE_COLORS[f.type] || '#666';
      dot.textContent = '● ';
      li.appendChild(dot);
      li.appendChild(document.createTextNode(f.name));
      listEl.appendChild(li);
    });
    document.getElementById('feature-panel-summary').textContent =
      `Named features along this route (${aggregateList.length})`;

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
