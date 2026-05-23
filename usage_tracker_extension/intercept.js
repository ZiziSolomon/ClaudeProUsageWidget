// Runs in MAIN world — can patch window.fetch directly.
// Intercepts ONLY the /api/organizations/<id>/usage response on the
// usage settings page and dispatches a DOM event for bridge.js. Other
// API responses are passed through untouched.

(function () {
  const USAGE_PATTERN = /\/api\/organizations\/[^/]+\/usage(\?|$)/;
  const _fetch = window.fetch.bind(window);

  window.fetch = async function (resource, init) {
    const response = await _fetch(resource, init);
    const url = typeof resource === "string" ? resource : resource.url;

    if (USAGE_PATTERN.test(url) && response.ok) {
      const clone = response.clone();
      try {
        const data = await clone.json();
        window.dispatchEvent(
          new CustomEvent("__claudeApiResponse__", {
            detail: { url, data },
          })
        );
      } catch (_) {}
    }

    return response;
  };
})();
