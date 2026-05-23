# Claude Usage Widget

A Windows tray widget that displays your current Claude.ai usage limits and
session reset times.

## Disclaimer

This is an **unofficial** tool with no affiliation to Anthropic. It works by
reading the logged-in browser session cookie and calling an undocumented
endpoint on `claude.ai`. That endpoint may change or stop working at any
time, and using it could conflict with Anthropic's Terms of Service. **Use at
your own risk** — including the risk of account action. If in doubt, don't
use it.

## Setup

1. Find your Anthropic organization ID:
   - Open <https://claude.ai/settings/usage> in your browser.
   - Open devtools → Network tab → reload.
   - Look for a request to `/api/organizations/<UUID>/usage`. The UUID is
     your org ID.
2. Configure it, either way:
   - **Env var**: set `CLAUDE_ORG_ID=<your-uuid>`.
   - **Config file**: copy `config.example.json` to `config.json` and fill in
     `org_id`. `config.json` is gitignored.
3. Install Python dependencies (`pip install -r usage_scraper/requirements.txt`
   plus `watchdog`, `browser_cookie3`, `curl_cffi`, `pystray`, `Pillow` for
   the tray widget).
4. Run `python widget_updater.py` (or build the standalone exe with
   PyInstaller — see `install_start_menu.ps1`).

## License

MIT — see [LICENSE](LICENSE).
