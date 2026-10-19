"""GTK4 / libadwaita widgets and UI helpers."""

import math
import os
import random

try:
    import cairo
except ImportError:
    cairo = None

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango
try:
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except (ImportError, ValueError):
    GdkPixbuf = None

LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1e3a8a"/>
      <stop offset="50%" stop-color="#0b1329"/>
      <stop offset="100%" stop-color="#020617"/>
    </linearGradient>
    <linearGradient id="chakra" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#38bdf8"/>
      <stop offset="50%" stop-color="#6366f1"/>
      <stop offset="100%" stop-color="#c084fc"/>
    </linearGradient>
    <linearGradient id="core" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#ffffff"/>
      <stop offset="50%" stop-color="#e0f2fe"/>
      <stop offset="100%" stop-color="#38bdf8"/>
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="3.5" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>
  <rect x="6" y="6" width="116" height="116" rx="28" fill="url(#bg)" stroke="#38bdf8" stroke-opacity="0.35" stroke-width="2"/>
  <circle cx="64" cy="64" r="44" fill="none" stroke="url(#chakra)" stroke-width="2.5" stroke-dasharray="10 6" opacity="0.55"/>
  <circle cx="64" cy="64" r="32" fill="none" stroke="#38bdf8" stroke-width="1.5" stroke-dasharray="4 4" opacity="0.4"/>
  <g filter="url(#glow)">
    <path d="M 64 22 L 86 52 L 72 52 L 72 72 L 56 72 L 56 52 L 42 52 Z" fill="url(#chakra)" opacity="0.85"/>
    <path d="M 64 34 Q 64 64 34 64 Q 64 64 64 94 Q 64 64 94 64 Q 64 64 64 34 Z" fill="url(#core)"/>
    <circle cx="64" cy="64" r="4.5" fill="#ffffff"/>
    <circle cx="64" cy="20" r="2.5" fill="#38bdf8"/>
    <circle cx="108" cy="64" r="2.5" fill="#818cf8"/>
    <circle cx="64" cy="108" r="2.5" fill="#c084fc"/>
    <circle cx="20" cy="64" r="2.5" fill="#38bdf8"/>
  </g>
</svg>"""

PALETTE_START = [(0.0, 0.95, 1.0), (0.2, 0.6, 1.0), (0.9, 1.0, 1.0), (0.35, 0.8, 1.0)]
PALETTE_CANCEL = [(1.0, 0.22, 0.15), (1.0, 0.45, 0.0), (1.0, 0.85, 0.25), (0.9, 0.1, 0.2)]
PALETTE_TEST = [(0.75, 0.25, 1.0), (0.9, 0.45, 1.0), (0.45, 0.15, 0.95), (1.0, 0.85, 1.0)]
PALETTE_ADD = [(0.1, 0.95, 0.45), (0.25, 1.0, 0.7), (0.85, 1.0, 0.9), (0.05, 0.85, 0.35)]
PALETTE_GOLD = [(1.0, 0.85, 0.0), (1.0, 0.65, 0.0), (1.0, 1.0, 0.7), (0.95, 0.75, 0.1)]

ICON_FOLDER = ("document-open-symbolic", "folder-open-symbolic", "folder-symbolic")
ICON_FILE = ("document-open-symbolic", "folder-open-symbolic")
ICON_REFRESH = ("view-refresh-symbolic", "emblem-synchronizing-symbolic")
ICON_TRASH = ("user-trash-symbolic", "edit-delete-symbolic", "window-close-symbolic")
ICON_STOP = ("media-playback-stop-symbolic", "process-stop-symbolic", "window-close-symbolic")
ICON_VIDEO = ("video-x-generic-symbolic", "emblem-videos-symbolic", "video-display-symbolic")
ICON_VIDEOS_EMPTY = ("folder-videos-symbolic", "video-x-generic-symbolic", "folder-symbolic")
ICON_LIST = ("view-list-symbolic", "open-menu-symbolic")
ICON_UP = ("go-up-symbolic", "pan-up-symbolic")
ICON_DOWN = ("go-down-symbolic", "pan-down-symbolic")


def get_logo_texture():
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(LOGO_SVG.encode("utf-8")))
    except Exception:
        return None


def load_image_pixbuf(path):
    if not path or not os.path.exists(path) or not GdkPixbuf:
        return None
    try:
        return GdkPixbuf.Pixbuf.new_from_file(str(path))
    except Exception:
        return None


def load_image_texture(path):
    if not path or not os.path.exists(path):
        return None
    pb = load_image_pixbuf(path)
    if pb is not None:
        try:
            return Gdk.Texture.new_for_pixbuf(pb)
        except Exception:
            pass
    try:
        return Gdk.Texture.new_from_filename(str(path))
    except Exception:
        return None


def pick_icon(*names):
    try:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for n in names:
            if theme.has_icon(n):
                return n
    except Exception:
        pass
    return names[-1]


# --------------------------------------------------------------------------
# Row helpers (titles are Pango markup in libadwaita: always escape user text)
# --------------------------------------------------------------------------
def esc(text):
    return GLib.markup_escape_text(str(text))


def group(title, description=None):
    g = Adw.PreferencesGroup()
    g.set_title(esc(title))
    if description:
        g.set_description(esc(description))
    return g


def icon_button(icon, tooltip, callback):
    b = Gtk.Button.new_from_icon_name(pick_icon(*icon) if isinstance(icon, tuple) else icon)
    b.add_css_class("flat")
    b.set_valign(Gtk.Align.CENTER)
    b.set_tooltip_text(tooltip)
    b.connect("clicked", lambda *_: callback())
    return b


def entry_row(title, text="", *buttons):
    r = Adw.EntryRow()
    r.set_title(esc(title))
    r.set_text(text or "")
    for b in buttons:
        r.add_suffix(b)
    return r


def combo(title, items, value=None, subtitle=None):
    r = Adw.ComboRow()
    r.set_title(esc(title))
    if subtitle:
        r.set_subtitle(esc(subtitle))
    r.set_model(Gtk.StringList.new(list(items)))
    if value in items:
        r.set_selected(list(items).index(value))
    return r


def combo_value(row):
    item = row.get_selected_item()
    return item.get_string() if item else ""


def combo_set(row, items, value=None):
    row.get_model().splice(0, row.get_model().get_n_items(), list(items))
    if value in items:
        row.set_selected(list(items).index(value))


def spin(title, lo, hi, step, value, subtitle=None):
    adj = Gtk.Adjustment(value=value, lower=lo, upper=hi, step_increment=step,
                         page_increment=step * 10)
    r = Adw.SpinRow(adjustment=adj)
    r.set_title(esc(title))
    if subtitle:
        r.set_subtitle(esc(subtitle))
    return r


def switch(title, active, subtitle=None):
    r = Adw.SwitchRow()
    r.set_title(esc(title))
    if subtitle:
        r.set_subtitle(esc(subtitle))
    r.set_active(bool(active))
    return r


def stat_tile(caption):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.add_css_class("stat")
    box.set_hexpand(True)
    cap = Gtk.Label(label=caption.upper(), xalign=0)
    cap.add_css_class("stat-caption")
    val = Gtk.Label(label="—", xalign=0)
    val.add_css_class("stat-value")
    val.set_ellipsize(Pango.EllipsizeMode.END)
    box.append(cap)
    box.append(val)
    return box, val


def build_css(dark, opacity):
    op = max(0.35, min(1.0, opacity))
    if dark:
        fg, accent = "#eef3fa", "#7db3f0"
        grad = (f"linear-gradient(150deg, rgba(32, 58, 108, {op:.2f}) 0%, "
                f"rgba(14, 20, 36, {op:.2f}) 52%, rgba(7, 10, 20, {op:.2f}) 100%)")
        glass, glass_border = "rgba(255, 255, 255, 0.065)", "rgba(255, 255, 255, 0.11)"
        side, side_border = f"rgba(6, 10, 22, {op * 0.55:.2f})", "rgba(255, 255, 255, 0.07)"
        view, shadow = "rgba(3, 7, 16, 0.40)", "rgba(0, 0, 0, 0.30)"
        stat = "rgba(255, 255, 255, 0.07)"
    else:
        fg, accent = "#1b2536", "#2f5fa8"
        grad = (f"linear-gradient(150deg, rgba(226, 238, 255, {op:.2f}) 0%, "
                f"rgba(244, 247, 252, {op:.2f}) 55%, rgba(232, 240, 252, {op:.2f}) 100%)")
        glass, glass_border = "rgba(255, 255, 255, 0.62)", "rgba(40, 70, 120, 0.14)"
        side, side_border = f"rgba(255, 255, 255, {op * 0.45:.2f})", "rgba(40, 70, 120, 0.12)"
        view, shadow = "rgba(255, 255, 255, 0.55)", "rgba(30, 50, 90, 0.16)"
        stat = "rgba(60, 110, 180, 0.09)"
    return f"""
@define-color accent_bg_color #3c6eb4;
@define-color accent_color {accent};
@define-color accent_fg_color #ffffff;
@define-color window_bg_color transparent;
@define-color window_fg_color {fg};
@define-color view_bg_color {view};
@define-color view_fg_color {fg};

window.apisr-window {{ background-image: {grad}; }}

.sidebar-pane {{
    background-color: {side};
    border-right: 1px solid {side_border};
}}

.glass {{
    background-color: {glass};
    border: 1px solid {glass_border};
    border-radius: 18px;
    padding: 16px 20px;
    box-shadow: 0 8px 24px {shadow};
}}

.stat {{
    background-color: {stat};
    border-radius: 12px;
    padding: 8px 12px;
}}
.stat .stat-value {{ font-weight: 700; font-size: 1.08em; }}
.stat .stat-caption {{ font-size: 0.74em; opacity: 0.65; letter-spacing: 0.04em; }}

button.pill {{ padding: 10px 24px; font-weight: 600; border-radius: 999px; }}
button.suggested-action.pill {{
    background-image: linear-gradient(135deg, #3c6eb4, #51a2da);
    color: white;
    box-shadow: 0 4px 14px rgba(60, 110, 180, 0.40);
}}

textview.logview {{ background: transparent; }}
textview.plain-glow, textview.plain-glow text {{
    font-family: "Cantarell", "Ubuntu", "Segoe UI", sans-serif;
    font-size: 9.6pt;
    line-height: 1.5;
    text-shadow: 0 0 10px rgba(56, 189, 248, 0.40), 0 0 18px rgba(99, 102, 241, 0.25);
}}
textview.terminal, textview.terminal text {{
    font-family: "JetBrains Mono", "Source Code Pro", "DejaVu Sans Mono", monospace;
    font-size: 9.2pt;
    text-shadow: none;
}}

.log-scroller {{ background-color: {view}; border-radius: 12px; }}

.preview-box {{
    background-color: rgba(0, 0, 0, 0.35);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 14px;
    overflow: hidden;
}}
"""


# --------------------------------------------------------------------------
# A/B split comparison with cached scaling, zoom and pan
# --------------------------------------------------------------------------
class SplitCompareArea(Gtk.DrawingArea):
    """Left of the divider: original. Right: upscaled.

    Both images are laid out into the SAME display rectangle (the upscaled frame is
    larger than the source, so each one gets its own scale factor). At "fit" zoom the
    scaled pixbufs are cached, so dragging the divider never rescales full-size images.

    Controls: left-drag moves the divider; scroll wheel zooms at the pointer; middle- or
    right-drag pans; double-click resets to fit; the "1:1" button shows upscaled pixels
    at native size.
    """

    MAX_ZOOM = 32.0

    def __init__(self):
        super().__init__()
        self.pix_old = None
        self.pix_new = None
        self.split_pos = 0.5
        self.zoom = 1.0
        self.cx, self.cy = 0.5, 0.5
        self._cache = {}
        self._pointer = (0.0, 0.0)
        self._pan_origin = None
        self.set_content_height(300)
        self.set_size_request(-1, 300)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_split)
        drag.connect("drag-update", self._on_split_update)
        self._split_start = (0.0, 0.0)
        self.add_controller(drag)

        for btn in (2, 3):
            pan = Gtk.GestureDrag.new()
            pan.set_button(btn)
            pan.connect("drag-begin", self._on_pan_begin)
            pan.connect("drag-update", self._on_pan_update)
            self.add_controller(pan)

        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", lambda g, n, x, y: self.reset_view() if n == 2 else None)
        self.add_controller(click)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", lambda c, x, y: setattr(self, "_pointer", (x, y)))
        self.add_controller(motion)

    # -- public ------------------------------------------------------------
    def set_images(self, old_path, new_path):
        self.pix_old = load_image_pixbuf(old_path)
        self.pix_new = load_image_pixbuf(new_path)
        self._cache.clear()
        self.reset_view()

    def reset_view(self):
        self.zoom, self.cx, self.cy = 1.0, 0.5, 0.5
        self.queue_draw()

    def zoom_actual(self):
        """Show the upscaled image at 1 image pixel per screen pixel."""
        if not self.pix_new:
            return
        w, h = self.get_width(), self.get_height()
        bx, by, bw, bh = self._base_rect(w, h)
        if bw > 0:
            self.zoom = max(1.0, min(self.MAX_ZOOM, self.pix_new.get_width() / bw))
            self.cx, self.cy = 0.5, 0.5
            self.queue_draw()

    # -- geometry ------------------------------------------------------------
    def _base_rect(self, width, height):
        pw, ph = self.pix_old.get_width(), self.pix_old.get_height()
        s = min(width / float(pw), height / float(ph))
        bw, bh = pw * s, ph * s
        return (width - bw) / 2.0, (height - bh) / 2.0, bw, bh

    def _layout(self, width, height):
        bx, by, bw, bh = self._base_rect(width, height)
        dw, dh = bw * self.zoom, bh * self.zoom
        dx = width / 2.0 - self.cx * dw
        dy = height / 2.0 - self.cy * dh
        # Clamp so the image can't be dragged out of view (or off-centre when smaller).
        dx = (width - dw) / 2.0 if dw <= width else min(0.0, max(width - dw, dx))
        dy = (height - dh) / 2.0 if dh <= height else min(0.0, max(height - dh, dy))
        self.cx = (width / 2.0 - dx) / dw
        self.cy = (height / 2.0 - dy) / dh
        return dx, dy, dw, dh

    # -- input -----------------------------------------------------------------
    def _on_split(self, gesture, x, y):
        self._set_split(x)

    def _on_split_update(self, gesture, ox, oy):
        ok, sx, sy = gesture.get_start_point()
        self._set_split(sx + ox if ok else ox)

    def _set_split(self, x):
        w = self.get_width()
        if w > 0:
            self.split_pos = max(0.02, min(0.98, x / float(w)))
            self.queue_draw()

    def _on_pan_begin(self, gesture, x, y):
        self._pan_origin = (self.cx, self.cy)

    def _on_pan_update(self, gesture, ox, oy):
        if not self.pix_old or self._pan_origin is None:
            return
        w, h = self.get_width(), self.get_height()
        bx, by, bw, bh = self._base_rect(w, h)
        dw, dh = bw * self.zoom, bh * self.zoom
        if dw > 0 and dh > 0:
            self.cx = self._pan_origin[0] - ox / dw
            self.cy = self._pan_origin[1] - oy / dh
            self.queue_draw()

    def _on_scroll(self, ctrl, dx, dy):
        if not self.pix_old:
            return False
        w, h = self.get_width(), self.get_height()
        dx0, dy0, dw, dh = self._layout(w, h)
        px, py = self._pointer
        u = (px - dx0) / dw
        v = (py - dy0) / dh
        factor = 0.8 if dy > 0 else 1.25
        new_zoom = max(1.0, min(self.MAX_ZOOM, self.zoom * factor))
        bx, by, bw, bh = self._base_rect(w, h)
        ndw, ndh = bw * new_zoom, bh * new_zoom
        self.zoom = new_zoom
        self.cx = (w / 2.0 - px) / ndw + u
        self.cy = (h / 2.0 - py) / ndh + v
        self.queue_draw()
        return True

    # -- drawing ---------------------------------------------------------------
    def _scaled(self, which, pix, w, h):
        key = (which, w, h)
        hit = self._cache.get(key)
        if hit is None:
            hit = pix.scale_simple(max(w, 1), max(h, 1), GdkPixbuf.InterpType.HYPER)
            if len(self._cache) > 6:
                self._cache.clear()
            self._cache[key] = hit
        return hit

    def _paint_image(self, cr, which, pix, rect, clip_x0, clip_x1, height):
        dx, dy, dw, dh = rect
        cr.save()
        cr.rectangle(clip_x0, 0, clip_x1 - clip_x0, height)
        cr.clip()
        if abs(self.zoom - 1.0) < 1e-3:
            sp = self._scaled(which, pix, int(round(dw)), int(round(dh)))
            Gdk.cairo_set_source_pixbuf(cr, sp, round(dx), round(dy))
            cr.paint()
        else:
            cr.translate(dx, dy)
            cr.scale(dw / pix.get_width(), dh / pix.get_height())
            Gdk.cairo_set_source_pixbuf(cr, pix, 0, 0)
            src = cr.get_source()
            src.set_filter(cairo.FILTER_NEAREST if dw / pix.get_width() > 1.5 else cairo.FILTER_GOOD)
            cr.paint()
        cr.restore()

    def _draw(self, area, cr, width, height):
        if width <= 0 or height <= 0:
            return
        cr.set_source_rgba(0.04, 0.07, 0.15, 0.85)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        if not self.pix_old or not self.pix_new or not cairo or not GdkPixbuf:
            cr.set_source_rgba(0.2, 0.4, 0.7, 0.4)
            cr.set_line_width(1.0)
            for i in range(0, width, 40):
                cr.move_to(i, 0)
                cr.line_to(i, height)
            for j in range(0, height, 40):
                cr.move_to(0, j)
                cr.line_to(width, j)
            cr.stroke()
            if cairo:
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(14)
                cr.set_source_rgba(0.7, 0.85, 1.0, 0.85)
                msg = "Render Test Frame or Start Upscaling to view interactive A/B comparison"
                ext = cr.text_extents(msg)
                cr.move_to(max((width - ext.width) / 2, 8), (height + ext.height) / 2)
                cr.show_text(msg)
            return

        rect = self._layout(width, height)
        x_split = width * self.split_pos
        self._paint_image(cr, "old", self.pix_old, rect, 0, x_split, height)
        self._paint_image(cr, "new", self.pix_new, rect, x_split, width, height)

        cr.save()
        laser = cairo.LinearGradient(x_split - 4, 0, x_split + 4, 0)
        laser.add_color_stop_rgba(0.0, 0.2, 0.7, 1.0, 0.0)
        laser.add_color_stop_rgba(0.5, 0.2, 0.9, 1.0, 0.95)
        laser.add_color_stop_rgba(1.0, 0.2, 0.7, 1.0, 0.0)
        cr.set_source(laser)
        cr.rectangle(x_split - 3, 0, 6, height)
        cr.fill()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        cr.set_line_width(1.5)
        cr.move_to(x_split, 0)
        cr.line_to(x_split, height)
        cr.stroke()

        cy = height / 2.0
        cr.arc(x_split, cy, 14, 0, math.pi * 2)
        cr.set_source_rgba(0.08, 0.15, 0.30, 0.9)
        cr.fill_preserve()
        cr.set_source_rgba(0.2, 0.85, 1.0, 0.9)
        cr.set_line_width(2.0)
        cr.stroke()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(10)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        cr.move_to(x_split - 8, cy + 3.5)
        cr.show_text("◀▶")

        cr.set_font_size(11)
        cr.set_source_rgba(0.0, 0.8, 1.0, 0.85)
        cr.move_to(16, 24)
        cr.show_text("ORIGINAL (SOURCE)")
        cr.set_source_rgba(0.75, 0.5, 1.0, 0.85)
        txt = "APISR REMASTER"
        cr.move_to(width - cr.text_extents(txt).width - 16, 24)
        cr.show_text(txt)
        if self.zoom > 1.001:
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.8)
            cr.move_to(16, height - 14)
            cr.show_text(f"{self.zoom:.1f}x  ·  double-click to reset")
        cr.restore()


# --------------------------------------------------------------------------
# Animated progress bar (stops redrawing when idle or finished)
# --------------------------------------------------------------------------
class ChakraProgressBar(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.fraction = 0.0
        self.pulse = False
        self.pulse_phase = 0.0
        self.flow_offset = 0.0
        self.particles = []
        self.set_content_height(26)
        self.set_size_request(-1, 26)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._on_tick)

    def set_fraction(self, frac):
        self.pulse = False
        self.fraction = max(0.0, min(1.0, float(frac)))
        self.queue_draw()

    def set_pulse(self, active=True):
        self.pulse = active
        self.queue_draw()

    def burst(self, colors, count=45):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        origin_x = max(10, w * self.fraction) if not self.pulse else w * 0.5
        origin_y = h / 2.0
        for _ in range(count):
            p_x = origin_x + random.uniform(-10, 10)
            p_y = origin_y + random.uniform(-h * 0.4, h * 0.4)
            angle = random.uniform(0, math.pi * 2)
            speed = random.uniform(2.5, 7.5)
            vx = math.cos(angle) * speed
            vy = math.sin(angle) * (speed * 0.5)
            life = random.randint(22, 45)
            size = random.uniform(3.0, 6.5)
            self.particles.append([p_x, p_y, vx, vy, size, life, life, random.choice(colors)])
        self.queue_draw()

    def _on_tick(self, widget, frame_clock):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return GLib.SOURCE_CONTINUE
        running = self.pulse or 0.0 < self.fraction < 1.0
        if not running and not self.particles:
            return GLib.SOURCE_CONTINUE  # idle: no animation, no redraws, no CPU
        self.flow_offset = (self.flow_offset + 0.045) % (math.pi * 2)
        if self.pulse:
            self.pulse_phase = (self.pulse_phase + 0.055) % (math.pi * 2)
            head_x = (math.sin(self.pulse_phase) * 0.44 + 0.5) * w
        else:
            head_x = w * self.fraction
        if running and head_x > 6:
            for _ in range(2):
                p_x = head_x + random.uniform(-4, 3)
                p_y = (h / 2.0) + random.uniform(-h * 0.35, h * 0.35)
                life = random.randint(16, 32)
                self.particles.append([p_x, p_y, random.uniform(-2.2, 0.4), random.uniform(-1.0, 1.0),
                                       random.uniform(2.0, 4.2), life, life, random.choice(PALETTE_START)])
        alive = []
        for p in self.particles:
            p[0] += p[2]
            p[1] += p[3]
            p[2] *= 0.96
            p[3] *= 0.96
            p[5] -= 1
            if p[5] > 0 and 0 <= p[0] <= w and 0 <= p[1] <= h:
                alive.append(p)
        self.particles = alive
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, area, cr, width, height):
        if width <= 0 or height <= 0:
            return
        r = height / 2.0

        def rounded_rect(x, y, w, h, rad):
            cr.new_sub_path()
            cr.arc(x + w - rad, y + rad, rad, -math.pi / 2, 0)
            cr.arc(x + w - rad, y + h - rad, rad, 0, math.pi / 2)
            cr.arc(x + rad, y + h - rad, rad, math.pi / 2, math.pi)
            cr.arc(x + rad, y + rad, rad, math.pi, 3 * math.pi / 2)
            cr.close_path()

        rounded_rect(0, 0, width, height, r)
        cr.set_source_rgba(0.06, 0.10, 0.22, 0.75)
        cr.fill_preserve()
        cr.set_source_rgba(0.0, 0.75, 1.0, 0.35)
        cr.set_line_width(1.0)
        cr.stroke()

        bar_w = width if self.pulse else width * self.fraction
        if bar_w <= 4:
            return
        cr.save()
        rounded_rect(0, 0, width, height, r)
        cr.clip()

        if self.pulse:
            pulse_x = (math.sin(self.pulse_phase) * 0.44 + 0.5) * width
            if cairo:
                grad = cairo.LinearGradient(pulse_x - 140, 0, pulse_x + 140, 0)
                grad.add_color_stop_rgba(0.0, 0.05, 0.2, 0.5, 0.0)
                grad.add_color_stop_rgba(0.5, 0.0, 0.9, 1.0, 0.85)
                grad.add_color_stop_rgba(1.0, 0.05, 0.2, 0.5, 0.0)
                cr.rectangle(0, 0, width, height)
                cr.set_source(grad)
                cr.fill()
            else:
                cr.set_source_rgba(0.0, 0.8, 1.0, 0.7)
                cr.rectangle(pulse_x - 40, 0, 80, height)
                cr.fill()
        else:
            if cairo:
                grad = cairo.LinearGradient(0, 0, bar_w, 0)
                grad.add_color_stop_rgba(0.0, 0.10, 0.30, 0.85, 0.95)
                grad.add_color_stop_rgba(0.55, 0.0, 0.75, 1.0, 1.0)
                grad.add_color_stop_rgba(0.92, 0.40, 0.95, 1.0, 1.0)
                grad.add_color_stop_rgba(1.0, 0.95, 1.0, 1.0, 1.0)
                rounded_rect(0, 0, bar_w, height, r)
                cr.set_source(grad)
                cr.fill()
            else:
                cr.set_source_rgba(0.0, 0.85, 1.0, 0.95)
                rounded_rect(0, 0, bar_w, height, r)
                cr.fill()

            cr.save()
            cr.set_line_width(2.0)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.50)
            cr.new_sub_path()
            for step in range(0, int(bar_w), 4):
                y_wave = (height / 2.0) + math.sin((step * 0.04) + self.flow_offset * 3) * (height * 0.22)
                if step == 0:
                    cr.move_to(step, y_wave)
                else:
                    cr.line_to(step, y_wave)
            cr.stroke()
            cr.restore()

            if bar_w > r:
                burst_r = r * (1.1 + 0.25 * math.sin(self.flow_offset * 4))
                if cairo:
                    rg = cairo.RadialGradient(bar_w - r / 2, height / 2.0, 1,
                                              bar_w - r / 2, height / 2.0, burst_r * 1.5)
                    rg.add_color_stop_rgba(0.0, 1.0, 1.0, 1.0, 0.95)
                    rg.add_color_stop_rgba(0.45, 0.0, 0.9, 1.0, 0.65)
                    rg.add_color_stop_rgba(1.0, 0.0, 0.5, 1.0, 0.0)
                    cr.arc(bar_w - r / 2, height / 2.0, burst_r * 1.5, 0, math.pi * 2)
                    cr.set_source(rg)
                    cr.fill()
                else:
                    cr.arc(bar_w - r / 2, height / 2.0, r * 0.8, 0, math.pi * 2)
                    cr.set_source_rgba(1.0, 1.0, 1.0, 0.8)
                    cr.fill()

        for px, py, vx, vy, sz, life, max_life, col in self.particles:
            alpha = (life / float(max_life)) * 0.95
            curr_sz = sz * (life / float(max_life))
            cr.arc(px, py, curr_sz, 0, math.pi * 2)
            cr.set_source_rgba(col[0], col[1], col[2], alpha)
            cr.fill()
        cr.restore()
