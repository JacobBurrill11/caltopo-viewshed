/*
 * Polls GET /routes/<route_id>/status and drives the progress bar on
 * processing.html until the route is done (redirect to its results page)
 * or failed (show the error box).
 *
 * The status endpoint returns JSON shaped like:
 *   {"status": "running" | "done" | "error",
 *    "completed": <int>, "total": <int>, "message": <string|null>}
 * "running" is the only state you need to re-poll for; "done" and "error"
 * are terminal.
 *
 * window.ROUTE_ID (set by processing.html) is the route_id to poll.
 */

function showError(message) {
  // Used for both a real "error" status from the server AND a poll that
  // never got a usable status at all -- both leave the user stuck looking
  // at a progress bar that will never move again, so both need the same
  // "stop, explain, offer a way out" treatment. Hides the whole track (not
  // just the fill) and the now-stale readout, so nothing half-finished is
  // left on screen next to the error box.
  document.getElementById('bar-track').style.display = 'none';
  document.getElementById('readout').style.display = 'none';
  document.getElementById('error').style.display = 'block';
  document.getElementById('error-message').textContent = message;
}

async function pollStatus() {
  let status;
  try {
    const response = await fetch(`/routes/${window.ROUTE_ID}/status`);
    status = await response.json();
  } catch (err) {
    // A genuine network-level failure (fetch itself throws only for this --
    // an HTTP error status like 404 still resolves normally and is handled
    // in the "unknown" branch below). Nothing useful to retry into here, so
    // surface it instead of letting the polling loop die silently.
    showError(`Lost connection while checking progress: ${err}`);
    return;
  }

  const barFill = document.getElementById('bar-fill');
  const readout = document.getElementById('readout');

  if (status.status === 'running') {
    const percentage = Math.round((status.completed / status.total) * 100);
    barFill.style.width = `${percentage}%`;
    readout.textContent = `Sample ${status.completed} of ${status.total}`;
    setTimeout(pollStatus, 1000); // Poll again after 1 second
  } else if (status.status === 'done') {
    window.location.href = `/results/${window.ROUTE_ID}`;
  } else if (status.status === 'error') {
    showError(status.message || 'An unknown error occurred.');
  } else {
    // "unknown" (returned with a 404 when the server has no record of this
    // route_id) or any other unrecognized status. Most likely cause in this
    // app: the debug reloader restarted the server mid-run, wiping the
    // in-memory progress store -- a known limitation, not a bug to chase.
    showError("Lost track of this route's progress (the server may have restarted). " +
               'Check the dashboard to see if it finished, or try uploading again.');
  }
}

pollStatus();
