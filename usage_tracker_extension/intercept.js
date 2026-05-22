// Runs in MAIN world — can patch window.fetch directly.
// Intercepts all claude.ai API responses on the usage page and
// dispatches a DOM event for bridge.js to relay to the background.

(function () {
  console.log("[claude-usage-tracker] intercept.js loaded");
  const _fetch = window.fetch.bind(window);

  window.fetch = async function (resource, init) {
    const response = await _fetch(resource, init);
    const url = typeof resource === "string" ? resource : resource.url;

    console.log("[claude-usage-tracker] fetch:", url);

    if (url.includes("/api/") && response.ok) {
      const clone = response.clone();
      try {
        const data = await clone.json();
        console.log("[claude-usage-tracker] dispatching event for:", url, data);
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
