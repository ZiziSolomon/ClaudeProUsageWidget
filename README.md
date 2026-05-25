> **UNOFFICIAL — NOT affiliated with Anthropic.**
> This tool works by replaying your logged-in browser session cookie against an **undocumented** `claude.ai` endpoint.
> Anthropic's deliberate vagueness about per-session token limits may be intentional; they may not welcome a tool
> that surfaces it. **Use at your own risk, including the risk of account action.** Decide for yourself whether
> that tradeoff is worth it. If you want a second opinion, you can always ask Claude directly about the risks.

---

# Claude Usage Widget

A Windows system-tray widget that shows your Claude.ai session and weekly usage in real time.
Ghost icon fills up as your quota is consumed; turns red at 90%.

---

## Check your usage from the terminal

The fastest way to see how much session is left — for yourself, or for Claude Code to query mid-task:

```
python usage_check.py            # show widget's current estimate (no network)
python usage_check.py --live     # fetch authoritative numbers from claude.ai
python usage_check.py --json     # machine-readable JSON (works with both)
```

`--live` hits the same endpoint Claude Code uses when it reports "X% of your session used", so the numbers match exactly.
This works on any OS where Python and a supported browser (Firefox or Chrome) are installed — no tray required.

---

## What it tracks — and what it doesn't

The widget combines two sources:

- **Web check (~every 10 minutes):** polls `claude.ai` for your actual Pro/Max utilisation percentage.
- **Live estimate (between web checks):** counts tokens from Claude Code conversations on **this device** to keep the reading current between polls.

**It does NOT see:**
- Usage from other devices
- Claude.ai web chat usage
- Claude Code usage on other devices

All of those will be picked up on the next web check (~10 minutes), but the live estimate between checks only reflects this device's Claude Code activity.

---

## Setup

### Option A — prebuilt (no Python needed, Windows only)

1. Download the latest `ClaudeUsage-win64.zip` from the [Releases page](../../releases).
2. Unzip anywhere.
3. Double-click `ClaudeUsage.exe`.

The org ID is auto-detected from your logged-in browser session — no configuration needed in most cases.

### Option B — from source

```
git clone <this-repo>
cd ClaudeUsageWidget
pip install -r requirements.txt
python tray_widget.py
```

### Finding your org ID (fallback only)

The widget auto-detects your org ID from your browser session.
If auto-detection fails, you can find it manually:

1. Open <https://claude.ai/settings/usage> in your browser.
2. Open DevTools → Network tab → reload the page.
3. Look for a request to `/api/organizations/<UUID>/usage`. Copy the UUID.
4. Either:
   - Set the environment variable `CLAUDE_ORG_ID=<your-uuid>`, or
   - Copy `config.example.json` to `config.json` and fill in `org_id`.

`config.json` is gitignored and will not be committed.

---

## Making the icon always visible

### Start at login

The tray menu has a **"Start at login"** toggle. Alternatively, copy the shortcut manually:

1. Press `Win+R`, type `shell:startup`, press Enter.
2. Copy a shortcut to `ClaudeUsage.exe` into that folder.

### Pin it to the taskbar tray (un-hide the icon)

Windows hides new tray icons under the `^` overflow by default. To keep it visible:

1. **Settings → Personalization → Taskbar**
2. Scroll to **"Other system tray icons"**
3. Toggle **"Claude Usage"** on.

---

## Platform support

| Platform | Tray widget | `usage_check.py` |
|----------|-------------|-----------------|
| Windows  | Yes (prebuilt exe + source) | Yes |
| macOS    | Not packaged yet | Yes (Python + browser) |
| Linux    | Not packaged yet | Yes (Python + browser) |

On macOS/Linux, `python usage_check.py --live` works anywhere Python and a supported browser are installed.
The tray and packaging haven't been shipped for non-Windows platforms yet, but the core is cross-platform.

---

## License

MIT — see [LICENSE](LICENSE).
