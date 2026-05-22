// Runs in ISOLATED world — has extension host_permissions, so can fetch localhost directly.
// Listens for API response events from intercept.js and POSTs usage data to the local server.
// Bypasses the service worker entirely to avoid message-dropping when it's inactive.

const SERVER_URL = "http://localhost:7432/usage";

window.addEventListener("__claudeApiResponse__", (event) => {
  const { url, data } = event.detail;

  const looksLikeUsage =
    url.includes("usage") ||
    "message_limit" in data ||
    "tokens_used" in data ||
    "usage" in data ||
    "token_usage" in data ||
    "period" in data;

  if (!looksLikeUsage) return;

  const payload = {
    scraped_at: new Date().toISOString(),
    source_url: url,
    raw: data,
  };

  fetch(SERVER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(() => {
    chrome.runtime.sendMessage({ type: "USAGE_CAPTURED" });
  }).catch((err) => {
    console.warn("Claude usage tracker: could not reach local server.", err);
  });
});
