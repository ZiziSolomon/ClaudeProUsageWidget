"""Generate an accuracy chart: continuous local estimate line + claude.ai
endpoint-reading dots.

Three ways to pick which session to plot:

    python save_accuracy_chart.py --last
        Pick the most recent session that appears in calibration.jsonl.

    python save_accuracy_chart.py --at "2026-05-27 09:30"
        Pick the session that was in flight at the given local datetime.
        Rejects with a non-zero exit if no recorded session covered it.
        Accepts any ISO-ish format datetime.fromisoformat() understands;
        bare dates are treated as 00:00 local on that day.

    python save_accuracy_chart.py
        Default = --last (most useful for debugging today's session).

The session window is taken from calibration.jsonl's `session_start` field
(the same window the widget itself uses), extended by SESSION_HOURS. Output
is written to docs/accuracy_sample.png unless --out points elsewhere.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DATA = Path(os.environ["LOCALAPPDATA"]) / "ClaudeUsage" / "usage_data"
JSONL = DATA / "calibration.jsonl"
LOG   = DATA / "widget_run.log"

# Must match widget_updater.SESSION_HOURS. Duplicated rather than imported so
# this script stays runnable without the widget's heavy import surface.
SESSION_HOURS = 5


def to_local_naive(dt: datetime) -> datetime:
    """Convert a UTC-aware datetime to a naive local-time datetime for plotting."""
    return datetime.fromtimestamp(dt.timestamp())


# Maximum gap between two session_starts that are still considered the same
# real session. Widget restarts re-derive session_start = resets_at - SESSION_HOURS;
# if resets_at drifts across restarts the stored start shifts too. 5 minutes is
# safely below the minimum real gap between sessions (SESSION_HOURS = 5h).
SESSION_MERGE_SECS = 300

# Gridline INTERVALS (see --hgrid / --vgrid). These are spacings, not counts:
# a horizontal line every HGRID percent, a vertical line every VGRID minutes.
# 10% reads cleanly against the 0-100% usage axis; 15-minute time gridlines
# give good resolution across a 5-hour session without crowding the labels.
DEFAULT_HGRID = 10     # percent between horizontal gridlines
DEFAULT_VGRID = 15     # minutes between vertical gridlines

# Default colour for endpoint markers when not colouring by reason.
ENDPOINT_COLOUR = "#E84C4C"

# Map a stored calibration `trigger` to a (legend label, colour) for the
# "colour endpoint calls by reason" option. The triggers are recorded by
# widget_updater._append_calibration. Fixed-point shots (5/10/95%) collapse to
# one reason; the rest stay distinct per the user's choice.
_TRIGGER_STYLE = {
    "liveness":           ("20m since last call", "#4C9BE8"),
    "liveness_10ppdelta": ("10pp since last call", "#E88A4C"),
    "liveness_5pct":      ("passed fixed point", "#3FA34D"),
    "liveness_10pct":     ("passed fixed point", "#3FA34D"),
    "liveness_95pct":     ("passed fixed point", "#3FA34D"),
    "startup":            ("startup", "#9B59B6"),
    "force_refresh":      ("manual refresh", "#E84C4C"),
    "suspect":            ("auto re-anchor", "#E8C84C"),
    "scheduled":          ("scheduled", "#7F8C8D"),
}
_TRIGGER_FALLBACK = ("other", "#7F8C8D")


def trigger_style(trigger: str | None) -> tuple[str, str]:
    """(legend label, hex colour) for a calibration trigger; fallback for
    unknown/missing triggers (old records may predate the field)."""
    return _TRIGGER_STYLE.get(trigger or "", _TRIGGER_FALLBACK)


# A jump segment shorter than this many percentage points is treated as "no
# jump" and drawn as a dot instead. Comfortably below endpoint quantisation
# (readings land on whole/half percents) so genuine matches don't draw a stub.
JUMP_EPSILON_PP = 0.05


def estimate_at(local_pts: list[dict], ts: datetime) -> float | None:
    """Linearly interpolate the local-estimate value at time `ts`.

    `local_pts` is the parsed estimate series ({"ts","pct"}), assumed sorted by
    ts (it is, being read in log order). Returns None if `ts` lies outside the
    series (can't interpolate the estimate the reading is jumping *from*)."""
    if not local_pts or ts < local_pts[0]["ts"] or ts > local_pts[-1]["ts"]:
        return None
    prev = local_pts[0]
    for cur in local_pts:
        if cur["ts"] >= ts:
            span = (cur["ts"] - prev["ts"]).total_seconds()
            if span <= 0:
                return cur["pct"]
            frac = (ts - prev["ts"]).total_seconds() / span
            return prev["pct"] + frac * (cur["pct"] - prev["pct"])
        prev = cur
    return local_pts[-1]["pct"]


def horizontal_grid_ticks(ymax: float, step_pct: float) -> list[float]:
    """Y-axis tick positions for a horizontal gridline every `step_pct` percent,
    from 0 up to (and including) ymax. So step_pct=25 -> [0,25,50,75,100] for a
    100% axis, and step_pct=1 -> a line at every percent.

    step_pct <= 0 disables horizontal gridlines (returns []). Ticks are rounded
    to whole percent for clean axis labels; the final tick is clamped to ymax so
    the top band is bounded even when ymax isn't a multiple of the step."""
    if step_pct <= 0 or ymax <= 0:
        return []
    ticks = []
    v = 0.0
    while v < ymax:
        ticks.append(round(v))
        v += step_pct
    ticks.append(round(ymax))
    return ticks


def _load_all_records() -> list[dict]:
    """All calibration records with a parseable session_start, session keys
    clustered so restarts with slightly different session_starts are merged.

    Each record gets _session_key: the canonical (earliest) minute-truncated
    session_start for its cluster."""
    if not JSONL.exists():
        sys.exit(f"calibration.jsonl not found at {JSONL}")

    records = []
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("session_start"):
            continue
        try:
            ss = datetime.fromisoformat(r["session_start"])
        except ValueError:
            continue
        r["_session_start_dt"] = ss
        r["_trunc"] = ss.replace(second=0, microsecond=0)
        records.append(r)

    if not records:
        return records

    # Build cluster mapping: sort distinct truncated starts, fold any start
    # within SESSION_MERGE_SECS of the running canonical into that canonical.
    distinct = sorted({r["_trunc"] for r in records})
    canonical_map: dict[datetime, datetime] = {}
    current: datetime | None = None
    for s in distinct:
        if current is None or (s - current).total_seconds() > SESSION_MERGE_SECS:
            current = s
        canonical_map[s] = current

    for r in records:
        r["_session_key"] = canonical_map[r["_trunc"]]

    return records


def _resolve_session(args) -> datetime:
    """Pick the session-start (UTC, aware) to plot, per the CLI flags.

    --at picks the session whose [start, start+SESSION_HOURS) window covers
    the given local datetime. --last picks the most recent session_start
    that appears in the calibration log. Default is --last."""
    records = _load_all_records()
    if not records:
        sys.exit("calibration.jsonl is empty - nothing to plot.")

    if args.at:
        # Parse the user's datetime as local-naive, convert to UTC-aware.
        try:
            target_local = datetime.fromisoformat(args.at)
        except ValueError as e:
            sys.exit(f"Could not parse --at {args.at!r}: {e}")
        target_utc = target_local.astimezone(timezone.utc) if target_local.tzinfo \
                     else target_local.astimezone().astimezone(timezone.utc)
        window = timedelta(hours=SESSION_HOURS)
        seen = set()
        for r in records:
            key = r["_session_key"]
            if key in seen:
                continue
            seen.add(key)
            if key <= target_utc < key + window:
                return key
        sys.exit(f"No recorded session covered {args.at} "
                 f"(local). Try --last to see what's on file.")

    # --last (default): pick the session key belonging to the record with
    # the most recent scraped_at. Using scraped_at (not session_start)
    # because some tests have historically polluted the log with synthetic
    # future-dated session_start values (e.g. 2099-01-01); scraped_at is
    # always the real wall-clock time and reflects actual activity.
    latest = max(records, key=lambda r: r["scraped_at"])
    return latest["_session_key"]


def load_api_points(session_start: datetime) -> list[dict]:
    """Calibration records that belong to the chosen session AND have a
    resolved budget (i.e. were above the floor at fetch time, OR were
    sub-floor-blended after the 2026-05-28 calibration changes)."""
    points = []
    for r in _load_all_records():
        if r["_session_key"] != session_start:
            continue
        if r.get("implied_session_budget") and r.get("session_pct") is not None:
            points.append({
                "ts":  to_local_naive(datetime.fromisoformat(r["scraped_at"])),
                "pct": r["session_pct"],
                "trigger": r.get("trigger"),   # why this endpoint call fired
            })
    # Deduplicate by minute (multiple rapid calls at the same % are noise).
    seen, deduped = set(), []
    for p in points:
        key = p["ts"].strftime("%H:%M")
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    return deduped


def load_local_estimates(session_start: datetime) -> list[dict]:
    """Parse [YYYY-MM-DD HH:MM:SS] pct lines that fall in the session window."""
    if not LOG.exists():
        return []

    sess_local_start = to_local_naive(session_start)
    sess_local_end   = sess_local_start + timedelta(hours=SESSION_HOURS)

    line_pat = re.compile(
        r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*pct=(\d+(?:\.\d+)?)"
    )
    results = []
    for line in LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = line_pat.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if sess_local_start <= ts < sess_local_end:
            results.append({"ts": ts, "pct": float(m.group(2))})
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--last", action="store_true",
                   help="Plot the most recent session (default).")
    g.add_argument("--at", metavar="DATETIME",
                   help='Plot the session in flight at this local datetime, '
                        'e.g. "2026-05-28 09:30". ISO-ish formats accepted.')
    ap.add_argument("--out", type=Path,
                    help="Output PNG path. Default: docs/accuracy_<session-start>.png "
                         "(timestamped so repeat runs don't overwrite each other - "
                         "useful for debugging across multiple sessions). Pass "
                         "docs/accuracy_sample.png explicitly when regenerating "
                         "the README chart.")
    ap.add_argument("--no-open", action="store_true",
                    help="Save the PNG but do not open it.")
    ap.add_argument("--hgrid", type=int, default=DEFAULT_HGRID, metavar="PCT",
                    help="Horizontal gridline every PCT percent on the %% axis "
                         f"(default: {DEFAULT_HGRID}; e.g. 1 = a line at every "
                         "percent). 0 disables horizontal gridlines.")
    ap.add_argument("--vgrid", type=int, default=DEFAULT_VGRID, metavar="MIN",
                    help="Vertical gridline every MIN minutes on the time axis "
                         f"(default: {DEFAULT_VGRID}). 0 disables vertical "
                         "gridlines.")
    ap.add_argument("--no-endpoint-vlines", action="store_true",
                    help="Do not draw a vertical marker line at each claude.ai "
                         "endpoint point (on by default).")
    ap.add_argument("--endpoint-hlines", action="store_true",
                    help="Draw a horizontal marker line at each endpoint point's "
                         "%% level (off by default).")
    ap.add_argument("--jump-segments", action="store_true",
                    help="Draw each endpoint reading as a vertical segment from "
                         "the local estimate's value at that moment to the new "
                         "endpoint reading (the correction the estimate makes), "
                         "instead of a dot. Readings that match the estimate "
                         "(no jump) draw a dot the width of the estimate line.")
    ap.add_argument("--colour-by-reason", action="store_true",
                    help="Colour each claude.ai endpoint point (and its marker "
                         "lines) by why the call fired (interval / delta / fixed "
                         "point / etc). Off by default (all one colour).")
    args = ap.parse_args()

    session_start = _resolve_session(args)
    if args.out is None:
        stamp = to_local_naive(session_start).strftime("%Y-%m-%d_%H%M")
        args.out = Path(__file__).parent / "docs" / f"accuracy_{stamp}.png"
    api_pts   = load_api_points(session_start)
    local_pts = load_local_estimates(session_start)

    if not local_pts:
        sys.exit(f"No local estimate data found in the log for session "
                 f"starting {session_start.isoformat()}.")

    local_ts  = [p["ts"] for p in local_pts]
    local_pct = [p["pct"] for p in local_pts]

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(local_ts, local_pct, "-", color="#4C9BE8", lw=1.8,
            label="Local estimate (live)")

    # Per-point colour: if colouring by reason, each endpoint point takes the
    # colour of its trigger; otherwise all share ENDPOINT_COLOUR. We scatter one
    # group per (label, colour) so the legend lists ONLY the reasons that
    # actually occurred this session (no dead swatches).
    if args.colour_by_reason:
        groups: dict[tuple[str, str], list[dict]] = {}
        for p in api_pts:
            groups.setdefault(trigger_style(p["trigger"]), []).append(p)
    else:
        groups = {("claude.ai endpoint", ENDPOINT_COLOUR): api_pts}

    for (label, colour), pts in groups.items():
        if args.jump_segments:
            # Draw each reading as the vertical correction the estimate makes:
            # a segment from the local estimate's value at that instant up/down
            # to the endpoint reading. Where the estimate already matched (no
            # jump), or the reading sits outside the estimate series so there's
            # nothing to jump from, fall back to a dot the width of the estimate
            # line. label only attaches once so the legend lists each group once.
            labelled = False
            for p in pts:
                est = estimate_at(local_pts, p["ts"])
                lbl = label if not labelled else None
                if est is not None and abs(p["pct"] - est) >= JUMP_EPSILON_PP:
                    # Horizontal end-caps ("_" markers) at both ends so even a
                    # sub-pp jump reads as a deliberate bracketed mark rather
                    # than vanishing into the estimate line.
                    ax.plot([p["ts"], p["ts"]], [est, p["pct"]],
                            color=colour, lw=2.6, solid_capstyle="butt",
                            marker="_", markersize=9, markeredgewidth=2.6,
                            zorder=5, label=lbl)
                else:
                    ax.plot(p["ts"], p["pct"], marker="o", color=colour,
                            markersize=4, zorder=5, label=lbl)
                labelled = True
        else:
            ax.scatter([p["ts"] for p in pts], [p["pct"] for p in pts],
                       color=colour, s=80, zorder=5, label=label)
        # Optional marker lines dropped from each endpoint point: vertical (down
        # to the time axis) and/or horizontal (across to the % axis). Toggled
        # independently from the dashboard; coloured to match the point so a
        # colour-by-reason chart stays consistent. Vertical defaults on, h off.
        if not args.no_endpoint_vlines:
            for p in pts:
                ax.axvline(p["ts"], color=colour, lw=0.6, ls=":", alpha=0.5)
        if args.endpoint_hlines:
            for p in pts:
                ax.axhline(p["pct"], color=colour, lw=0.6, ls=":", alpha=0.5)
    api_pct = [p["pct"] for p in api_pts]   # still needed for the y-axis range

    sess_label = to_local_naive(session_start).strftime("%Y-%m-%d %H:%M")
    ax.set_title(f"Session {sess_label} - local estimate vs claude.ai endpoint readings",
                 fontsize=10)
    ax.set_ylabel("Usage %")
    ymax = max(max(local_pct), max(api_pct) if api_pct else 0) + 5
    ax.set_ylim(0, ymax)

    # Horizontal gridlines at even %-of-range steps, labelled on the y-axis so
    # the curve's level can be read at a glance instead of estimated.
    hticks = horizontal_grid_ticks(ymax, args.hgrid)
    if hticks:
        ax.set_yticks(hticks)
        ax.yaxis.set_major_formatter(lambda v, _pos: f"{v:.0f}%")
        ax.grid(axis="y", alpha=0.3)

    # Vertical gridlines every args.vgrid MINUTES of wall-clock. MinuteLocator
    # with byminute lets us place ticks at multiples of the interval (00,30,...
    # for vgrid=30); fall back to a plain interval for spacings that don't
    # divide 60 (e.g. 45) so we still get evenly-spaced lines.
    if args.vgrid > 0:
        if 60 % args.vgrid == 0:
            ax.xaxis.set_major_locator(
                mdates.MinuteLocator(byminute=range(0, 60, args.vgrid)))
        else:
            ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=args.vgrid))
        ax.grid(axis="x", alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.tick_params(axis="x", labelsize=9)
    ax.legend(fontsize=9)
    plt.tight_layout()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(args.out, dpi=140, bbox_inches="tight")
    print(f"Saved -> {args.out}")
    if not args.no_open:
        os.startfile(args.out)


if __name__ == "__main__":
    main()
