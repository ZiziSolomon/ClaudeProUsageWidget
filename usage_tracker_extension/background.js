const USAGE_URL = "https://claude.ai/settings/usage";
const SERVER_URL = "http://localhost:7432/usage";

// How often to open the usage page and capture data (in minutes).
// Runs a few times a day — adjust as needed once cron schedule is finalised.
const ALARM_INTERVAL_MINUTES = 240; // every 4 hours

// Set up the repeating alarm on install / browser start.
chrome.runtime.onInstalled.addListener(scheduleAlarm);
chrome.runtime.onStartup.addListener(scheduleAlarm);

function scheduleAlarm() {
  chrome.alarms.create("fetchUsage", {
    delayInMinutes: 1,
    periodInMinutes: ALARM_INTERVAL_MINUTES,
  });
}

// When the alarm fires, open the usage page in a background tab.
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "fetchUsage") {
    openUsageTab();
  }
});

function openUsageTab() {
  chrome.tabs.create({ url: USAGE_URL, active: false }, (tab) => {
    // Safety net: close after 30s regardless.
    setTimeout(() => {
      closeTabAndWindowIfEmpty(tab.id, tab.windowId);
    }, 30_000);
  });
}

// Close the tab immediately when bridge.js confirms data was captured.
chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.type === "USAGE_CAPTURED" && sender.tab) {
    closeTabAndWindowIfEmpty(sender.tab.id, sender.tab.windowId);
  }
});

function closeTabAndWindowIfEmpty(tabId, windowId) {
  chrome.tabs.remove(tabId, () => {
    if (chrome.runtime.lastError) return; // already closed
    // If the window has no remaining tabs, close it too.
    chrome.tabs.query({ windowId }, (remaining) => {
      if (remaining.length === 0) {
        chrome.windows.remove(windowId).catch(() => {});
      }
    });
  });
}

