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
 *
 * What to implement, in pollStatus() below:
 *   1. fetch(`/routes/${window.ROUTE_ID}/status`) and parse the JSON body.
 *   2. On "running": update #bar-fill's width to the completed/total
 *      percentage, and #readout's text to something like
 *      "Sample 4 of 12". A nice (optional) touch: track how long the
 *      poll loop has been running and the completed count over time to
 *      estimate a "~2 min remaining" from the actual observed pace,
 *      rather than a hardcoded per-sample constant -- more accurate,
 *      since real DEM fetch time varies with network conditions.
 *   3. On "done": redirect the whole page with
 *      `window.location.href = `/results/${window.ROUTE_ID}``.
 *   4. On "error": hide the progress bar, show #error, and set
 *      #error-message's text to the status response's "message" field.
 *   5. On a fetch/network failure itself (not a JSON "error" status --
 *      an actual failed request), decide how to handle it: a transient
 *      blip probably shouldn't give up on the first miss, but polling
 *      forever into a dead server isn't great either.
 *   6. Schedule the next poll (setTimeout or setInterval) at some
 *      reasonable interval -- polling once a second is a reasonable
 *      starting point, fast enough to feel responsive without hammering
 *      the server.
 *
 * Kick it off once at the bottom of this file (e.g. `pollStatus()`), the
 * same way viewer.js's own fetch() runs immediately on page load.
 */

function pollStatus() {
  throw new Error("processing.js: pollStatus() is not implemented yet");
}

pollStatus();
