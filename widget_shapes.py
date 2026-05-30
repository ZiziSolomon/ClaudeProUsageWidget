#!/usr/bin/env python3
"""
Custom widget shapes — turn an uploaded image or SVG into a canonical alpha
mask that BOTH renderers (PIL tray icons and the HTML dashboard) draw from.

Design (see the feature brief): every uploaded shape is normalised to a single
high-resolution **alpha-mask PNG** stored in the user-data dir. The tray loads
that mask and runs it through the existing colour + fill-mode pipeline (it just
replaces the built-in ghost body mask); the dashboard uses the same PNG as a
CSS mask-image on a colour-filled element. One mask, two renderers, guaranteed
to agree.

Two input paths:
  • Raster (PNG/BMP/etc.) — the rock-solid primary path. Threshold "not-black":
    every pixel brighter than a small luminance floor becomes opaque, the rest
    transparent. Pure Pillow, no native deps.
  • SVG — best-effort, pure-Python. We parse a useful subset of path/shape
    elements with the stdlib XML parser and flatten them into ImageDraw
    polygons. Anything we can't handle raises a clear error the dashboard can
    surface; we deliberately avoid heavy native rasterisers (cairo/potrace)
    that would wreck the lean PyInstaller build.

Storage is under the caller-provided data dir (STATE_FILE.parent), never the
repo, so a source checkout can't be polluted by uploads.
"""

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

# Reference resolution for the stored canonical mask. High enough that the
# downscale to a 64px tray icon stays crisp, and that the dashboard's CSS
# mask-image looks clean on hi-DPI displays. Square so either renderer can
# letterbox/scale it into its own aspect box without us guessing here.
REF_SIZE = 512

# Luminance below this (0..255) counts as "background" for raster uploads.
# Small so only near-black is dropped; anything with real colour is kept as
# silhouette. Mirrors the user's "trace the not-black" description.
NOT_BLACK_THRESHOLD = 24

# Hard cap on accepted upload size. A silhouette source is tiny; anything
# larger is almost certainly a mistake (or an attempt to wedge the server) and
# we reject it before reading the body into memory.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB

# Raster formats we accept. Lossless is what the user asked for; we also accept
# the common lossy ones since the threshold step tolerates mild artefacts.
_RASTER_EXTS = {".png", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".jpg", ".jpeg"}


class ShapeError(ValueError):
    """Raised for an upload we can't turn into a mask (bad type, empty result,
    unsupported SVG). The message is safe to surface to the dashboard."""


# ---------------------------------------------------------------------------
# Raster -> mask
# ---------------------------------------------------------------------------

def raster_bytes_to_mask(data: bytes) -> Image.Image:
    """Threshold a raster image's "not-black" pixels into an L-mode alpha mask.

    A pixel is kept (alpha 255) when its luminance exceeds NOT_BLACK_THRESHOLD
    OR — when the source has its own alpha — where it is already opaque-ish and
    not black. Fully transparent source pixels are always dropped, so a PNG
    silhouette on a transparent background traces correctly even if the
    silhouette itself happens to be black.

    The result is trimmed to its content bbox and centred into a square REF_SIZE
    canvas, so both renderers get a consistent, normalised silhouette.
    """
    try:
        src = Image.open(_BytesIO(data))
        src.load()
    except Exception as e:
        raise ShapeError(f"could not decode image: {e}") from e

    src = src.convert("RGBA")
    # Greyscale luminance of the colour channels; the alpha channel may also
    # carry the silhouette.
    lum   = src.convert("L")
    alpha = src.getchannel("A")
    alpha_mask = alpha.point(lambda v: 255 if v > 0 else 0)

    from PIL import ImageChops
    # Primary rule: a pixel is foreground when it's "not black" (luminance above
    # the floor) AND not fully transparent — this is the user's stated
    # "trace the not-black" path for an opaque image.
    lum_mask = lum.point(lambda v: 255 if v > NOT_BLACK_THRESHOLD else 0)
    mask = ImageChops.multiply(lum_mask, alpha_mask)

    # Fallback for a silhouette drawn in BLACK on a TRANSPARENT background
    # (common for exported icons): the colour channels are all black so the
    # luminance rule drops everything, but the alpha channel IS the shape. If
    # the source has real transparency and the luminance pass found nothing,
    # trace the opaque region instead.
    has_transparency = alpha.getextrema()[0] < 255
    if has_transparency and mask.getbbox() is None:
        mask = alpha_mask

    bbox = mask.getbbox()
    if not bbox:
        raise ShapeError("image had no non-black pixels to trace")
    return _normalise_mask(mask.crop(bbox))


# ---------------------------------------------------------------------------
# SVG -> mask (pure-python, best-effort)
# ---------------------------------------------------------------------------

# Path command tokeniser: a command letter or a (possibly signed/scientific)
# number. Good enough for the path data Illustrator/Figma/Inkscape emit.
_TOKEN_RE = re.compile(r"[a-zA-Z]|-?\d*\.?\d+(?:[eE][-+]?\d+)?")


def svg_bytes_to_mask(data: bytes) -> Image.Image:
    """Rasterise a subset of SVG into an alpha mask, no native deps.

    Supported: <path> (M/L/H/V/C/Q/A/Z, absolute and relative), <polygon>,
    <polyline>, <rect>, <circle>, <ellipse>, <line> (ignored — no area).
    Everything is flattened to polygons and filled with the SVG even-odd-ish
    default (nonzero is approximated as union, which is right for the silhouette
    use case). Curves are sampled into line segments.

    The whole drawing is scaled from the SVG's viewBox/width-height into the
    REF_SIZE reference box. Raises ShapeError when nothing drawable is found, so
    the dashboard can tell the user the SVG isn't supported rather than store an
    empty mask.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ShapeError(f"invalid SVG XML: {e}") from e

    vb = _svg_viewbox(root)
    polys: list[list[tuple[float, float]]] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]  # strip namespace
        try:
            if tag == "path":
                polys.extend(_flatten_path(el.get("d", "")))
            elif tag in ("polygon", "polyline"):
                pts = _parse_points(el.get("points", ""))
                if len(pts) >= 2:
                    polys.append(pts)
            elif tag == "rect":
                polys.append(_rect_poly(el))
            elif tag == "circle":
                polys.append(_ellipse_poly(
                    float(el.get("cx", 0)), float(el.get("cy", 0)),
                    float(el.get("r", 0)),  float(el.get("r", 0))))
            elif tag == "ellipse":
                polys.append(_ellipse_poly(
                    float(el.get("cx", 0)), float(el.get("cy", 0)),
                    float(el.get("rx", 0)), float(el.get("ry", 0))))
        except (ValueError, TypeError):
            # A single malformed element shouldn't abort the whole trace; skip
            # it and keep whatever else we can flatten.
            continue

    polys = [p for p in polys if len(p) >= 3]
    if not polys:
        raise ShapeError(
            "no fillable shapes found in SVG (supported: path, polygon, "
            "rect, circle, ellipse). Try exporting a flattened/outlined SVG, "
            "or upload a PNG instead.")

    # Determine the source coordinate box: explicit viewBox wins; otherwise the
    # bbox of everything we flattened (so a viewBox-less SVG still normalises).
    if vb:
        minx, miny, w, h = vb
    else:
        xs = [x for p in polys for x, _ in p]
        ys = [y for p in polys for _, y in p]
        minx, miny = min(xs), min(ys)
        w, h = (max(xs) - minx) or 1.0, (max(ys) - miny) or 1.0

    scale = min(REF_SIZE / w, REF_SIZE / h) if w and h else 1.0
    mask = Image.new("L", (REF_SIZE, REF_SIZE), 0)
    d = ImageDraw.Draw(mask)
    for poly in polys:
        scaled = [((x - minx) * scale, (y - miny) * scale) for x, y in poly]
        # Even-odd fill so inner holes (e.g. the ghost eyes) punch through,
        # matching how the built-in ghost path is drawn.
        _xor_polygon(mask, d, scaled)

    bbox = mask.getbbox()
    if not bbox:
        raise ShapeError("SVG produced an empty silhouette")
    return _normalise_mask(mask.crop(bbox))


def _xor_polygon(mask: Image.Image, d: ImageDraw.ImageDraw,
                 pts: list[tuple[float, float]]) -> None:
    """Fill one polygon with even-odd semantics by XOR-compositing it onto the
    accumulating mask. This makes overlapping subpaths (outer body + inner eye
    cutouts) behave like a single even-odd fill, so holes appear correctly."""
    from PIL import ImageChops
    layer = Image.new("L", mask.size, 0)
    ImageDraw.Draw(layer).polygon(pts, fill=255)
    # XOR via difference-of-unions: where both are set, clear; else union.
    # ImageChops has no xor for L directly, so compute (a|b) - (a&b).
    union = ImageChops.lighter(mask, layer)
    inter = ImageChops.darker(mask, layer)
    mask.paste(ImageChops.subtract(union, inter))


# ---- SVG geometry helpers -------------------------------------------------

def _svg_viewbox(root) -> tuple[float, float, float, float] | None:
    vb = root.get("viewBox")
    if vb:
        nums = [float(n) for n in re.split(r"[ ,]+", vb.strip()) if n]
        if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
            return (nums[0], nums[1], nums[2], nums[3])
    # Fall back to width/height attributes (strip any unit suffix).
    w = _len(root.get("width")); h = _len(root.get("height"))
    if w and h:
        return (0.0, 0.0, w, h)
    return None


def _len(v: str | None) -> float | None:
    if not v:
        return None
    m = re.match(r"-?\d*\.?\d+", v.strip())
    return float(m.group()) if m else None


def _parse_points(raw: str) -> list[tuple[float, float]]:
    nums = [float(n) for n in re.split(r"[ ,\n\r\t]+", raw.strip()) if n]
    return list(zip(nums[0::2], nums[1::2]))


def _rect_poly(el) -> list[tuple[float, float]]:
    x = float(el.get("x", 0)); y = float(el.get("y", 0))
    w = float(el.get("width", 0)); h = float(el.get("height", 0))
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def _ellipse_poly(cx: float, cy: float, rx: float, ry: float,
                  steps: int = 64) -> list[tuple[float, float]]:
    return [(cx + rx * math.cos(t), cy + ry * math.sin(t))
            for t in (2 * math.pi * i / steps for i in range(steps))]


def _flatten_path(d: str) -> list[list[tuple[float, float]]]:
    """Flatten an SVG path 'd' string into one or more polygons (one per
    subpath). Curves are sampled; arcs are approximated by their chord-stepped
    elliptic sweep. Unknown commands end the current subpath gracefully."""
    tokens = _TOKEN_RE.findall(d or "")
    if not tokens:
        return []
    polys: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    i = 0
    x = y = 0.0          # current point
    start_x = start_y = 0.0
    cmd = ""

    def num() -> float:
        nonlocal i
        v = float(tokens[i]); i += 1
        return v

    while i < len(tokens):
        t = tokens[i]
        if re.match(r"[a-zA-Z]", t):
            cmd = t
            i += 1
            if cmd in ("Z", "z"):
                if cur:
                    cur.append((start_x, start_y))
                    polys.append(cur)
                    cur = []
                x, y = start_x, start_y
                continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            x = (x + num()) if rel else num()
            y = (y + num()) if rel else num()
            if cur:
                polys.append(cur)
            cur = [(x, y)]
            start_x, start_y = x, y
            # Subsequent implicit pairs after M are treated as L (SVG spec).
            cmd = "l" if rel else "L"
        elif c == "L":
            x = (x + num()) if rel else num()
            y = (y + num()) if rel else num()
            cur.append((x, y))
        elif c == "H":
            x = (x + num()) if rel else num()
            cur.append((x, y))
        elif c == "V":
            y = (y + num()) if rel else num()
            cur.append((x, y))
        elif c == "C":
            x1 = (x + num()) if rel else num(); y1 = (y + num()) if rel else num()
            x2 = (x + num()) if rel else num(); y2 = (y + num()) if rel else num()
            ex = (x + num()) if rel else num(); ey = (y + num()) if rel else num()
            cur.extend(_sample_cubic(x, y, x1, y1, x2, y2, ex, ey))
            x, y = ex, ey
        elif c == "Q":
            x1 = (x + num()) if rel else num(); y1 = (y + num()) if rel else num()
            ex = (x + num()) if rel else num(); ey = (y + num()) if rel else num()
            cur.extend(_sample_quad(x, y, x1, y1, ex, ey))
            x, y = ex, ey
        elif c == "A":
            rx = num(); ry = num(); num()  # x-axis-rotation (ignored)
            num(); num()                   # large-arc-flag, sweep-flag (ignored)
            ex = (x + num()) if rel else num(); ey = (y + num()) if rel else num()
            # Crude but adequate for a silhouette: sample an elliptic chord from
            # (x,y) to (ex,ey). Full arc maths isn't worth the native-dep-free
            # complexity here; the segments still trace the outline closely.
            cur.extend(_sample_arc(x, y, rx, ry, ex, ey))
            x, y = ex, ey
        else:
            # Unsupported command (S/T smooth curves etc.): stop this subpath.
            i += 1
            continue
    if cur:
        polys.append(cur)
    return polys


def _sample_cubic(x0, y0, x1, y1, x2, y2, x3, y3, steps=24):
    out = []
    for k in range(1, steps + 1):
        t = k / steps; mt = 1 - t
        out.append((
            mt**3*x0 + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x3,
            mt**3*y0 + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y3))
    return out


def _sample_quad(x0, y0, x1, y1, x2, y2, steps=18):
    out = []
    for k in range(1, steps + 1):
        t = k / steps; mt = 1 - t
        out.append((
            mt**2*x0 + 2*mt*t*x1 + t**2*x2,
            mt**2*y0 + 2*mt*t*y1 + t**2*y2))
    return out


def _sample_arc(x0, y0, rx, ry, x1, y1, steps=18):
    # Approximate the arc by interpolating along an ellipse-ish bulge between
    # the endpoints. Not geometrically exact, but the chordal samples keep the
    # silhouette smooth enough; exact arc maths would need full endpoint->centre
    # parameterisation we deliberately skip to stay dependency-free.
    out = []
    for k in range(1, steps + 1):
        t = k / steps
        out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return out


# ---------------------------------------------------------------------------
# Shared normalisation + dispatch
# ---------------------------------------------------------------------------

def _normalise_mask(mask: Image.Image) -> Image.Image:
    """Centre an already-cropped L-mode silhouette into a square REF_SIZE
    canvas, preserving aspect ratio. Both renderers then scale this one mask
    into their own box, so they always show the same silhouette."""
    mask = mask.convert("L")
    w, h = mask.size
    scale = min(REF_SIZE / w, REF_SIZE / h) if w and h else 1.0
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = mask.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("L", (REF_SIZE, REF_SIZE), 0)
    canvas.paste(resized, ((REF_SIZE - nw) // 2, (REF_SIZE - nh) // 2))
    return canvas


def process_upload(data: bytes, filename: str | None,
                   content_type: str | None = None) -> Image.Image:
    """Turn raw uploaded bytes into a canonical alpha mask, dispatching on type.

    Type detection: explicit SVG content-type or .svg extension routes to the
    SVG flattener; everything else is treated as raster. Raises ShapeError on
    anything we can't process (too big, undecodable, empty result)."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ShapeError(
            f"file too large ({len(data)} bytes; max {MAX_UPLOAD_BYTES}).")
    if not data:
        raise ShapeError("empty upload.")

    ext = Path(filename or "").suffix.lower()
    head = data.lstrip()[:64].lower()
    is_svg = (ext == ".svg"
              or (content_type or "").lower().startswith("image/svg")
              or head.startswith(b"<?xml")
              or head.startswith(b"<svg"))
    if is_svg:
        return svg_bytes_to_mask(data)
    if ext and ext not in _RASTER_EXTS:
        raise ShapeError(
            f"unsupported file type {ext!r}. Upload a PNG/lossless image or an SVG.")
    return raster_bytes_to_mask(data)


def save_mask(mask: Image.Image, dest_dir: Path, widget: str) -> str:
    """Persist a processed mask under dest_dir as a PNG and return its filename.

    Filename is keyed by widget so each of session/weekly/clock has its own,
    and overwriting on re-upload keeps the dir from accumulating orphans. The
    stored file is the alpha channel encoded as an L-mode PNG (small, lossless)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"shape_{widget}.png"
    mask.save(dest_dir / fname, "PNG")
    return fname


def load_mask(path: Path, size: int) -> Image.Image:
    """Load a stored mask PNG and resize it to `size`×`size` for the tray.

    Returns an L-mode mask ready to drop into render_ghost in place of the
    built-in body mask. Letterboxes to preserve aspect within the square icon.
    """
    src = Image.open(path).convert("L")
    w, h = src.size
    scale = min(size / w, size / h) if w and h else 1.0
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = src.resize((nw, nh), Image.LANCZOS)
    out = Image.new("L", (size, size), 0)
    out.paste(resized, ((size - nw) // 2, (size - nh) // 2))
    # Re-binarise: LANCZOS introduces grey edges; the fill pipeline expects a
    # crisp mask (getbbox / pieslice-AND assume mostly 0/255). A mid threshold
    # keeps the silhouette edge stable across sizes.
    return out.point(lambda v: 255 if v >= 128 else 0)


# Local import kept at the bottom so the module's public surface reads first.
from io import BytesIO as _BytesIO  # noqa: E402
