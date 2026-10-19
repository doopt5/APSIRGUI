"""Main window and application (GTK4 + libadwaita)."""

import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from . import __version__
from .config import (APP_ID, BUILTIN_PROFILES, DEFAULT_FRAME_FORMAT, DEVICE_AUTO, DEVICE_CPU,
                     ENCODERS, FPS_SMOOTH_OPTIONS, MODELS_CACHE_DIR, ON_DONE_OPTIONS, PRESETS,
                     PROFILE_KEYS, Config, load_config, save_config)
from .logtext import translate_to_plain_english
from .pipeline import (FRAME_FORMATS, Cancelled, Pipeline, describe_commands, plan_summary,
                       render_test_frame, validate_cfg)
from .system import (APISR_WEIGHTS, ARNNDN_FILES, ARNNDN_REPO, GUI_MODULES, NO_AUTO_INSTALL,
                     PIP_NAMES, arch_ok, detect_torch, download_verified,
                     ensure_desktop_integration, find_missing_modules, find_pythons,
                     fmt_time, github_blob_sha1, github_expected_sha256, guess_apisr_dir,
                     local_import_closure, setup_nvrtc_env, torch_install_hint)
from .widgets import (ICON_DOWN, ICON_FILE, ICON_FOLDER, ICON_LIST, ICON_REFRESH, ICON_STOP,
                      ICON_TRASH, ICON_UP, ICON_VIDEO, ICON_VIDEOS_EMPTY, LOGO_SVG,
                      PALETTE_ADD, PALETTE_CANCEL, PALETTE_GOLD, PALETTE_START, PALETTE_TEST,
                      ChakraProgressBar, SplitCompareArea, build_css, combo, combo_set,
                      combo_value, entry_row, esc, get_logo_texture, group, icon_button,
                      load_image_texture, pick_icon, spin, stat_tile, switch)

HEIGHT_LABELS = {"720": "720p", "1080": "1080p (Full HD)", "1440": "1440p", "2160": "2160p (4K)",
                 "keep": "Keep upscaled size"}
APPEARANCES = {"system": "Follow system", "dark": "Dark", "light": "Light"}
CUSTOM_PROFILE = "Custom (current settings)"


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.cfg = load_config(find_pythons, guess_apisr_dir)
        self.run_cfg = None
        self.q = queue.Queue()
        self.pipeline = None
        self.worker = None
        self.last_work = ""
        self.last_output = ""
        self.inhibit_cookie = 0
        self.videos = [v for v in self.cfg.videos if os.path.exists(v)]
        self.weight_map = {}
        self.queue_rows = []
        self._fields = {}
        self._profile_busy = False
        self._profile_entries = []

        self.verbose_log_items = []
        self.plain_log_items = []
        self.log_mode = "plain"

        self.set_title("APISR Upscaler")
        self.set_icon_name(APP_ID)
        self.set_default_size(1260, 920)
        self.set_size_request(420, 560)
        self.add_css_class("apisr-window")

        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

        self._build()
        self._install_actions()
        self._apply_theme()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._apply_theme())
        self._refresh_weights()
        self._refresh_profiles()
        self._rebuild_queue()
        GLib.timeout_add(100, self._poll)
        self.connect("close-request", self._on_close)
        self._append("Starting. Weights: " + (self.cfg.weight or "None"))

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self):
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        view = Adw.ToolbarView()
        view.set_top_bar_style(Adw.ToolbarStyle.FLAT)
        self.toasts.set_child(view)

        header = Adw.HeaderBar()
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        logo_tex = get_logo_texture()
        if logo_tex:
            logo_img = Gtk.Image.new_from_paintable(logo_tex)
            logo_img.set_pixel_size(24)
            title_box.append(logo_img)
        title_box.append(Adw.WindowTitle.new("APISR Upscaler", "Anime Super-Resolution & Remastering"))
        header.set_title_widget(title_box)

        self.sidebar_toggle = Gtk.ToggleButton()
        self.sidebar_toggle.set_icon_name("view-sidebar-start-symbolic")
        self.sidebar_toggle.set_tooltip_text("Show / hide settings")
        header.pack_start(self.sidebar_toggle)

        add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add_btn.set_tooltip_text("Add videos to queue")
        add_btn.connect("clicked", lambda *_: self.bar.burst(PALETTE_ADD, count=35))
        add_btn.set_action_name("win.add-videos")
        header.pack_start(add_btn)

        menu = Gio.Menu()
        menu.append("Detect devices", "win.detect-devices")
        menu.append("Check / install packages", "win.check-packages")
        menu.append("Copy command lines (first video)", "win.copy-commands")
        menu.append("Show APISR --help", "win.show-help")
        menu.append("Environment setup help", "win.setup-help")
        sec = Gio.Menu()
        sec.append("Open frames folder", "win.open-frames")
        sec.append("About", "win.about")
        menu.append_section(None, sec)
        mb = Gtk.MenuButton()
        mb.set_icon_name("open-menu-symbolic")
        mb.set_menu_model(menu)
        mb.set_primary(True)
        header.pack_end(mb)
        view.add_top_bar(header)

        self.split = Adw.OverlaySplitView()
        self.split.set_min_sidebar_width(320)
        self.split.set_max_sidebar_width(750)
        self.split.set_sidebar_width_fraction(0.33)
        self.split.set_sidebar(self._build_sidebar())
        self.split.set_content(self._build_content())
        self.split.bind_property("show-sidebar", self.sidebar_toggle, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        view.set_content(self.split)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 820sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

    def _field(self, key, row, kind):
        self._fields[key] = (row, kind)
        return row

    def _build_sidebar(self):
        c = self.cfg
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_hexpand(True)
        for m in ("start", "end"):
            getattr(box, f"set_margin_{m}")(18)
        box.set_margin_top(12)
        box.set_margin_bottom(24)
        self.sidebar_box = box

        # 0. Profiles
        pg = group("Profiles", "Presets for common jobs; save your own")
        self.profile_row = combo("Profile", [CUSTOM_PROFILE], CUSTOM_PROFILE)
        self.profile_row.add_suffix(icon_button(("document-save-symbolic", "list-add-symbolic"),
                                                "Save current settings as a profile", self._save_profile))
        self.profile_del_btn = icon_button(ICON_TRASH, "Delete the selected saved profile",
                                           self._delete_profile)
        self.profile_row.add_suffix(self.profile_del_btn)
        self.profile_row.connect("notify::selected", self._on_profile_selected)
        pg.add(self.profile_row)
        box.append(pg)

        # 1. Engine
        g = group("Engine", "APISR and Python environment")
        self.apisr_row = self._field("apisr_dir", entry_row(
            "APISR folder", c.apisr_dir,
            icon_button(ICON_FOLDER, "Browse", lambda: self._pick_folder(
                "Select APISR folder", lambda p: self.apisr_row.set_text(p)))), "entry")
        self.apisr_row.connect("notify::text", lambda *_: self._refresh_weights())
        g.add(self.apisr_row)

        self.weight_row = combo("Model weights", [], subtitle="2x models recommended for 720p/1080p")
        self.weight_row.add_suffix(icon_button(ICON_FILE, "Browse for .pth file", self._pick_weight))
        g.add(self.weight_row)

        env_btn = Gtk.MenuButton()
        env_btn.set_icon_name(pick_icon(*ICON_LIST))
        env_btn.add_css_class("flat")
        env_btn.set_valign(Gtk.Align.CENTER)
        env_btn.set_tooltip_text("Detected Python environments")
        env_btn.set_create_popup_func(self._make_env_popover)
        self.python_row = self._field("python", entry_row(
            "Python for APISR", c.python, env_btn,
            icon_button(ICON_FILE, "Browse", lambda: self._pick_file(
                "Select python interpreter of APISR env", lambda p: self.python_row.set_text(p)))), "entry")
        g.add(self.python_row)

        self.extra_row = self._field("extra", entry_row("Extra CLI args", c.extra), "entry")
        self.extra_row.set_tooltip_text("Manual flags passed to inference.py")
        g.add(self.extra_row)
        box.append(g)

        # 2. CUDA
        cg = group("CUDA Options", "NVIDIA silicon, TensorRT, and hardware acceleration")
        saved = c.device
        items = [DEVICE_AUTO, DEVICE_CPU] + ([saved] if saved.startswith("GPU") else [])
        self.device_row = self._field("device", combo("Compute device", items, saved), "combo")
        self.device_row.add_suffix(icon_button(ICON_REFRESH, "Detect devices", self._detect_devices))
        cg.add(self.device_row)

        self.device_info = Adw.ActionRow()
        self.device_info.set_title("CUDA Architecture")
        self.device_info.set_subtitle("Press detect to view hardware features.")
        self.device_info.set_subtitle_lines(0)
        self.device_info.set_subtitle_selectable(True)
        cg.add(self.device_info)

        cg.add(self._field("fp16", switch("Half precision (FP16)", c.fp16,
                                          "Runs on Tensor Cores (much faster, cuts VRAM in half)"), "switch"))
        cg.add(self._field("nvdec", switch("NVDEC hardware video decoding", c.nvdec,
                                           "Decodes input frames on GPU silicon instead of CPU"), "switch"))
        cg.add(self._field("cuda_filters", switch("CUDA hardware video filters", c.cuda_filters,
                                                  "scale_cuda for resizing. Skips deband, sharpen and grain"),
                           "switch"))
        cg.add(self._field("tensorrt", switch("TensorRT inference mode", c.tensorrt,
                                              "Only if your inference.py has a --tensorrt flag"), "switch"))
        cg.add(self._field("persistent_worker", switch(
            "Persistent model worker (experimental)", c.persistent_worker,
            "Keeps the model loaded between chunks. Falls back automatically if it can't start"), "switch"))
        self.gpustats_row = self._field("gpu_stats", switch("Show GPU usage in tiles", c.gpu_stats,
                                                            "Reads NVML (pip install nvidia-ml-py) or nvidia-smi"),
                                        "switch")
        cg.add(self.gpustats_row)
        box.append(cg)

        # 3. Output & filters
        g = group("Output & Anime Filters")
        self.height_row = self._field("height", combo(
            "Target resolution", list(HEIGHT_LABELS.values()),
            HEIGHT_LABELS.get(str(c.height), HEIGHT_LABELS["1080"])), "height")
        g.add(self.height_row)
        g.add(self._field("encoder", combo("Encoder", ENCODERS, c.encoder,
                                           "NVENC encodes on the GPU hardware chip (much faster)"), "combo"))
        g.add(self._field("preset", combo("Preset", PRESETS, c.preset, "x264 / x265 only"), "combo"))
        g.add(self._field("crf", spin("Quality (CRF)", 0, 51, 1, int(c.crf),
                                      "Lower = better visual quality, larger file size"), "spin"))
        g.add(self._field("tenbit", switch("10-bit color depth", c.tenbit,
                                           "Drastically reduces banding in gradients"), "switch"))
        g.add(self._field("deinterlace", switch("Deinterlace (interlaced sources only)", c.deinterlace,
                                                "For 480i DVD anime. Only touches frames flagged interlaced"), "switch"))
        g.add(self._field("deband", switch("Deband gradients", c.deband,
                                           "Smooths harsh color steps in anime skies and flat aura colors"), "switch"))
        g.add(self._field("sharpen", switch("Adaptive line sharpening", c.sharpen,
                                            "Sharpens dark line art edges after downscaling"), "switch"))
        self.out_row = self._field("out_dir", entry_row(
            "Output folder", c.out_dir,
            icon_button(ICON_FOLDER, "Browse", lambda: self._pick_folder(
                "Select output folder", lambda p: self.out_row.set_text(p)))), "entry")
        self.out_row.set_tooltip_text("Leave empty to save next to source videos")
        g.add(self.out_row)
        box.append(g)

        # 4. Advanced
        ga = group("Advanced Remastering & Processing", "Texture, audio, motion, and RAM-disk workflows")
        ga.add(self._field("hdr10", switch(
            "SDR to HDR10 expansion (experimental)", c.hdr10,
            "Maps SDR to PQ / BT.2020 at 203-nit white. Needs 10-bit HEVC/AV1. No new highlight detail"), "switch"))
        ga.add(self._field("grain", switch("Anime film grain injection", c.grain,
                                           "Restores organic texture and masks 8-bit gradient stepping"), "switch"))
        ga.add(self._field("derainbow", switch(
            "Chroma cleanup (anti-rainbow)", c.derainbow,
            "chromanr + chroma-only temporal denoise. Helps composite-video rainbows and color noise"), "switch"))
        ga.add(self._field("rife_fps", combo(
            "Frame interpolation (motion smoothing)", FPS_SMOOTH_OPTIONS, c.rife_fps,
            "Uses rife-ncnn-vulkan if on PATH, otherwise ffmpeg minterpolate (slower, lower quality)"), "combo"))
        ga.add(self._field("ai_audio", switch("Dialogue enhancement & noise suppression", c.ai_audio,
                                              "RNNoise + EQ + normalization. Re-encodes audio to Opus"), "switch"))
        ga.add(self._field("ramdisk", switch("RAM-disk temp frames (/dev/shm)", c.ramdisk,
                                             "Protects SSD wear. Used only if the chunk fits in free RAM"), "switch"))
        ga.add(self._field("sound_cues", switch("Milestone audio cues", c.sound_cues,
                                                "Plays a chime when renders complete"), "switch"))
        dl_row = Adw.ActionRow()
        dl_row.set_title("Model downloader")
        dl_row.set_subtitle("Official APISR release weights and RNNoise models, checksum-verified")
        dl_btn = Gtk.Button(label="Download models")
        dl_btn.add_css_class("pill")
        dl_btn.set_valign(Gtk.Align.CENTER)
        dl_btn.connect("clicked", lambda *_: (self.bar.burst(PALETTE_GOLD, count=50), self._download_models()))
        dl_row.add_suffix(dl_btn)
        ga.add(dl_row)
        box.append(ga)

        # 5. Streaming & automation
        g = group("Streaming & Automation")
        g.add(self._field("frame_format", combo(
            "Temporary frame format", list(FRAME_FORMATS), c.frame_format,
            "BMP gives the absolute fastest I/O speed"), "combo"))
        g.add(self._field("chunk", spin("Chunk size (frames)", 50, 20000, 50, int(c.chunk),
                                        "Smaller = less temporary disk space"), "spin"))
        self.work_row = self._field("work_dir", entry_row(
            "Frames folder", c.work_dir,
            icon_button(ICON_FOLDER, "Browse", lambda: self._pick_folder(
                "Select folder for temporary frames", lambda p: self.work_row.set_text(p))),
            icon_button(ICON_FILE, "Open in file manager", self._open_frames)), "entry")
        self.work_row.set_tooltip_text("Leave empty to use output directory")
        g.add(self.work_row)
        g.add(self._field("test_seconds", spin("Test clip duration (seconds)", 0, 3600, 10, int(c.test_seconds),
                                               "0 = process the whole video"), "spin"))
        g.add(self._field("keep", switch("Keep temporary frames", c.keep), "switch"))
        g.add(self._field("confirm_start", switch("Show summary before starting", c.confirm_start,
                                                  "Estimated disk use and time"), "switch"))
        g.add(self._field("on_done", combo("When finished", ON_DONE_OPTIONS, c.on_done,
                                           "Action to execute once all videos complete"), "combo"))
        box.append(g)

        # 6. Appearance
        g = group("Appearance")
        self.theme_row = self._field("appearance", combo(
            "Theme", list(APPEARANCES.values()), APPEARANCES.get(c.appearance)), "theme")
        self.theme_row.connect("notify::selected", lambda *_: self._apply_theme())
        g.add(self.theme_row)
        trow = Adw.ActionRow()
        trow.set_title("Window transparency")
        self.opacity_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.40, 1.0, 0.02)
        self.opacity_scale.set_value(float(c.opacity))
        self.opacity_scale.set_size_request(150, -1)
        self.opacity_scale.set_valign(Gtk.Align.CENTER)
        self.opacity_scale.set_draw_value(False)
        self.opacity_scale.connect("value-changed", lambda *_: self._apply_theme())
        trow.add_suffix(self.opacity_scale)
        g.add(trow)
        box.append(g)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_child(box)
        scroller.add_css_class("sidebar-pane")
        return scroller

    def _build_content(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_hexpand(True)
        outer.set_vexpand(True)
        for m in ("start", "end"):
            getattr(outer, f"set_margin_{m}")(20)
        outer.set_margin_top(12)
        outer.set_margin_bottom(24)

        self.queue_group = group("Processing Queue")
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        clear_q = Gtk.Button(label="Clear")
        clear_q.add_css_class("flat")
        clear_q.connect("clicked", lambda *_: self._clear_queue())
        add = Gtk.Button(label="Add videos…")
        add.add_css_class("flat")
        add.set_action_name("win.add-videos")
        hdr.append(clear_q)
        hdr.append(add)
        self.queue_group.set_header_suffix(hdr)
        outer.append(self.queue_group)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("glass")
        card.set_hexpand(True)
        self.title_label = Gtk.Label(label="Ready to upscale", xalign=0)
        self.title_label.add_css_class("title-3")
        self.title_label.set_wrap(True)
        card.append(self.title_label)
        self.extra_label = Gtk.Label(
            label="Load anime episodes, configure options in the sidebar, then press Start.", xalign=0)
        self.extra_label.add_css_class("dim-label")
        self.extra_label.set_wrap(True)
        card.append(self.extra_label)

        self.bar = ChakraProgressBar()
        card.append(self.bar)

        tiles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        tiles.set_hexpand(True)
        tiles.set_homogeneous(True)
        t1, self.v_speed = stat_tile("Speed")
        t2, self.v_eta = stat_tile("Time left")
        t3, self.v_gpu = stat_tile("GPU")
        t4, self.v_disk = stat_tile("Free disk")
        for t in (t1, t2, t3, t4):
            tiles.append(t)
        card.append(tiles)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_halign(Gtk.Align.END)
        self.test_1frame_btn = Gtk.Button(label="Render Test Frame")
        self.test_1frame_btn.add_css_class("pill")
        self.test_1frame_btn.set_tooltip_text("Processes 1 sample frame with your real filters to preview quality")
        self.test_1frame_btn.connect("clicked", lambda *_: (self.bar.burst(PALETTE_TEST, count=45),
                                                            self._test_single_frame()))
        row.append(self.test_1frame_btn)

        self.cancel_btn = Gtk.Button()
        self.cancel_btn.set_child(Adw.ButtonContent(icon_name=pick_icon(*ICON_STOP), label="Cancel"))
        self.cancel_btn.add_css_class("pill")
        self.cancel_btn.add_css_class("destructive-action")
        self.cancel_btn.set_sensitive(False)
        self.cancel_btn.connect("clicked", lambda *_: (self.bar.burst(PALETTE_CANCEL, count=55), self._cancel()))
        row.append(self.cancel_btn)

        self.start_btn = Gtk.Button()
        self.start_btn.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic",
                                                   label="Start upscaling"))
        self.start_btn.add_css_class("pill")
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", lambda *_: (self.bar.burst(PALETTE_START, count=60), self._start()))
        row.append(self.start_btn)
        card.append(row)
        outer.append(card)

        # Comparison card
        self.preview_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.preview_card.add_css_class("glass")
        self.preview_card.set_hexpand(True)
        phead = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        prev_title = Gtk.Label(label="Frame Comparison", xalign=0)
        prev_title.add_css_class("heading")
        prev_title.set_hexpand(True)
        phead.append(prev_title)

        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        zoom_box.add_css_class("linked")
        fit_btn = Gtk.Button(label="Fit")
        fit_btn.set_tooltip_text("Fit the whole frame (also: double-click)")
        fit_btn.connect("clicked", lambda *_: self.split_area.reset_view())
        one_btn = Gtk.Button(label="1:1")
        one_btn.set_tooltip_text("Native pixels. Scroll to zoom, middle/right-drag to pan")
        one_btn.connect("clicked", lambda *_: self.split_area.zoom_actual())
        zoom_box.append(fit_btn)
        zoom_box.append(one_btn)
        phead.append(zoom_box)

        view_modes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        view_modes.add_css_class("linked")
        self.btn_view_split = Gtk.ToggleButton(label="Split Slider ↔")
        self.btn_view_split.set_active(True)
        self.btn_view_side = Gtk.ToggleButton(label="Side by Side ◫")
        self.btn_view_split.connect("toggled", self._on_view_split_toggled)
        self.btn_view_side.connect("toggled", self._on_view_side_toggled)
        view_modes.append(self.btn_view_split)
        view_modes.append(self.btn_view_side)
        phead.append(view_modes)
        self.preview_card.append(phead)

        self.split_area = SplitCompareArea()
        self.preview_card.append(self.split_area)

        self.side_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.side_box.set_hexpand(True)
        self.side_box.set_homogeneous(True)
        self.side_box.set_visible(False)

        def pane(label_text):
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            lbl = Gtk.Label(label=label_text, xalign=0)
            lbl.add_css_class("stat-caption")
            wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            wrap.add_css_class("preview-box")
            wrap.set_size_request(-1, 260)
            pic = Gtk.Picture()
            pic.set_can_shrink(True)
            if hasattr(pic, "set_content_fit"):
                pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            wrap.append(pic)
            b.append(lbl)
            b.append(wrap)
            return b, pic

        box_old, self.pic_old = pane("Original (Source Frame)")
        box_new, self.pic_new = pane("Upscaled (APISR Remaster)")
        self.side_box.append(box_old)
        self.side_box.append(box_new)
        self.preview_card.append(self.side_box)
        outer.append(self.preview_card)

        # Log card
        logcard = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        logcard.add_css_class("glass")
        logcard.set_hexpand(True)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        lbl = Gtk.Label(label="Activity Log", xalign=0)
        lbl.add_css_class("heading")
        lbl.set_hexpand(True)
        head.append(lbl)
        modes_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        modes_box.add_css_class("linked")
        self.btn_log_plain = Gtk.ToggleButton(label="Chakra Guide ✨")
        self.btn_log_plain.set_active(True)
        self.btn_log_verbose = Gtk.ToggleButton(label="Terminal CLI 💻")
        self.btn_log_plain.connect("toggled", self._on_log_mode_plain)
        self.btn_log_verbose.connect("toggled", self._on_log_mode_verbose)
        modes_box.append(self.btn_log_plain)
        modes_box.append(self.btn_log_verbose)
        head.append(modes_box)
        clear = Gtk.Button(label="Clear")
        clear.add_css_class("flat")
        clear.connect("clicked", self._clear_logs)
        head.append(clear)
        logcard.append(head)

        self.log_buf = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buf)
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("logview")
        self.log_view.add_css_class("plain-glow")
        self.log_view.set_left_margin(12)
        self.log_view.set_right_margin(12)
        self.log_view.set_top_margin(10)
        self.log_view.set_bottom_margin(10)
        sc = Gtk.ScrolledWindow()
        sc.set_min_content_height(140)
        sc.set_max_content_height(180)
        sc.set_vexpand(False)
        sc.set_hexpand(True)
        sc.set_child(self.log_view)
        sc.add_css_class("log-scroller")
        logcard.append(sc)
        outer.append(logcard)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_child(outer)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        scroller.add_controller(drop)
        return scroller

    # ------------------------------------------------------------------
    # View toggles and log
    # ------------------------------------------------------------------
    def _on_view_split_toggled(self, btn):
        if btn.get_active():
            self.btn_view_side.set_active(False)
            self.split_area.set_visible(True)
            self.side_box.set_visible(False)

    def _on_view_side_toggled(self, btn):
        if btn.get_active():
            self.btn_view_split.set_active(False)
            self.split_area.set_visible(False)
            self.side_box.set_visible(True)

    def _on_log_mode_plain(self, btn):
        if btn.get_active():
            self.btn_log_verbose.set_active(False)
            self.log_mode = "plain"
            self._render_log_view()

    def _on_log_mode_verbose(self, btn):
        if btn.get_active():
            self.btn_log_plain.set_active(False)
            self.log_mode = "verbose"
            self._render_log_view()

    def _clear_logs(self, *_):
        self.verbose_log_items.clear()
        self.plain_log_items.clear()
        self.log_buf.set_text("")

    def _insert_markup(self, markup):
        end = self.log_buf.get_end_iter()
        if hasattr(self.log_buf, "insert_markup"):
            self.log_buf.insert_markup(end, markup + "\n", -1)
        else:
            self.log_buf.insert(end, re.sub(r"<[^>]+>", "", markup) + "\n")

    def _trim_buffer(self, keep_lines=1000, max_lines=1500):
        """Keep the on-screen TextBuffer bounded (the item lists were already capped)."""
        if self.log_buf.get_line_count() > max_lines:
            start = self.log_buf.get_start_iter()
            cut = self.log_buf.get_iter_at_line(self.log_buf.get_line_count() - keep_lines)
            if isinstance(cut, tuple):  # older PyGObject returns (ok, iter)
                cut = cut[-1]
            self.log_buf.delete(start, cut)

    def _scroll_log_to_end(self):
        adj = self.log_view.get_vadjustment()
        if adj:
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def _render_log_view(self):
        self.log_buf.set_text("")
        if self.log_mode == "plain":
            self.log_view.remove_css_class("terminal")
            self.log_view.add_css_class("plain-glow")
            for item in self.plain_log_items[-400:]:
                self._insert_markup(item)
        else:
            self.log_view.remove_css_class("plain-glow")
            self.log_view.add_css_class("terminal")
            self.log_buf.set_text("\n".join(self.verbose_log_items[-1000:]) + "\n")
        self._scroll_log_to_end()

    def _append(self, text):
        self.verbose_log_items.append(text)
        plain = translate_to_plain_english(text)
        if plain:
            self.plain_log_items.append(plain)
        if len(self.verbose_log_items) > 3000:
            self.verbose_log_items = self.verbose_log_items[-2000:]
        if len(self.plain_log_items) > 1500:
            self.plain_log_items = self.plain_log_items[-1000:]
        if self.log_mode == "verbose":
            self.log_buf.insert(self.log_buf.get_end_iter(), text + "\n")
        elif plain:
            self._insert_markup(plain)
        self._trim_buffer()
        self._scroll_log_to_end()

    # ------------------------------------------------------------------
    # Actions, theme, dialogs
    # ------------------------------------------------------------------
    def _install_actions(self):
        def act(name, fn):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: fn())
            self.add_action(a)
        act("add-videos", self._add_videos)
        act("detect-devices", self._detect_devices)
        act("check-packages", self._check_packages)
        act("copy-commands", self._copy_commands)
        act("show-help", self._show_help)
        act("setup-help", self._setup_help)
        act("open-frames", self._open_frames)
        act("open-output", self._open_output)
        act("about", self._about)

    def _apply_theme(self):
        scheme = {v: k for k, v in APPEARANCES.items()}.get(combo_value(self.theme_row), "system")
        sm = Adw.StyleManager.get_default()
        sm.set_color_scheme({"system": Adw.ColorScheme.DEFAULT, "dark": Adw.ColorScheme.FORCE_DARK,
                             "light": Adw.ColorScheme.FORCE_LIGHT}[scheme])
        css = build_css(sm.get_dark(), self.opacity_scale.get_value())
        if hasattr(self.css, "load_from_string"):
            self.css.load_from_string(css)
        else:
            self.css.load_from_data(css.encode())

    def _toast(self, text, action=None, label=None):
        t = Adw.Toast.new(text)
        if action:
            t.set_button_label(label or "Open")
            t.set_action_name(action)
        self.toasts.add_toast(t)

    def _alert(self, heading, body):
        dlg = Adw.AlertDialog.new(heading, body)
        dlg.add_response("ok", "OK")
        dlg.set_default_response("ok")
        dlg.present(self)

    def _notify(self, title, body):
        """Native notification via GApplication; notify-send only as a fallback."""
        try:
            n = Gio.Notification.new(title)
            n.set_body(body)
            n.set_icon(Gio.ThemedIcon.new(APP_ID))
            n.set_default_action("app.present")
            self.get_application().send_notification("apisr-finished", n)
            return
        except Exception:
            pass
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body])

    def _inhibit_sleep(self, reason):
        if self.inhibit_cookie:
            return
        try:
            self.inhibit_cookie = self.get_application().inhibit(
                self, Gtk.ApplicationInhibitFlags.SUSPEND | Gtk.ApplicationInhibitFlags.IDLE, reason)
        except Exception:
            self.inhibit_cookie = 0

    def _uninhibit_sleep(self):
        if self.inhibit_cookie:
            try:
                self.get_application().uninhibit(self.inhibit_cookie)
            except Exception:
                pass
            self.inhibit_cookie = 0

    # ------------------------------------------------------------------
    # Settings <-> widgets, profiles
    # ------------------------------------------------------------------
    @staticmethod
    def _set_combo_to(row, value):
        model = row.get_model()
        for i in range(model.get_n_items()):
            if model.get_string(i) == value:
                row.set_selected(i)
                return True
        return False

    def _read_field(self, key):
        row, kind = self._fields[key]
        if kind == "entry":
            return row.get_text().strip()
        if kind == "switch":
            return row.get_active()
        if kind == "spin":
            return int(row.get_value())
        if kind == "combo":
            return combo_value(row)
        if kind == "height":
            return {v: k for k, v in HEIGHT_LABELS.items()}.get(combo_value(row), "1080")
        if kind == "theme":
            return {v: k for k, v in APPEARANCES.items()}.get(combo_value(row), "system")
        return None

    def _write_field(self, key, value):
        row, kind = self._fields[key]
        if kind == "entry":
            row.set_text(str(value))
        elif kind == "switch":
            row.set_active(bool(value))
        elif kind == "spin":
            row.set_value(float(value))
        elif kind == "combo":
            self._set_combo_to(row, value)
        elif kind == "height":
            self._set_combo_to(row, HEIGHT_LABELS.get(str(value), HEIGHT_LABELS["1080"]))
        elif kind == "theme":
            self._set_combo_to(row, APPEARANCES.get(value, APPEARANCES["system"]))

    def _apply_settings(self, d):
        for k, v in d.items():
            if k in self._fields:
                self._write_field(k, v)

    def _gather(self):
        cfg = Config()
        for key in self._fields:
            setattr(cfg, key, self._read_field(key))
        cfg.python = cfg.python or sys.executable
        cfg.weight = self.weight_map.get(combo_value(self.weight_row), "")
        cfg.opacity = round(self.opacity_scale.get_value(), 2)
        cfg.videos = list(self.videos)
        cfg.profiles = dict(self.cfg.profiles)
        cfg.bench = dict(self.cfg.bench)
        return cfg

    def _refresh_profiles(self, select=None):
        ents = [(CUSTOM_PROFILE, None)]
        ents += [(n, d) for n, d in BUILTIN_PROFILES.items()]
        ents += [(f"★ {n}", d) for n, d in sorted(self.cfg.profiles.items())]
        self._profile_entries = ents
        self._profile_busy = True
        combo_set(self.profile_row, [e[0] for e in ents], select or CUSTOM_PROFILE)
        self._profile_busy = False
        self._update_profile_buttons()

    def _selected_profile_label(self):
        return combo_value(self.profile_row)

    def _update_profile_buttons(self):
        self.profile_del_btn.set_sensitive(self._selected_profile_label().startswith("★ "))

    def _on_profile_selected(self, *_):
        if self._profile_busy:
            return
        label = self._selected_profile_label()
        self._update_profile_buttons()
        for name, data in self._profile_entries:
            if name == label and data:
                self._apply_settings(data)
                self._toast(f"Applied profile: {label.lstrip(chr(0x2605)).strip()}")
                break

    def _save_profile(self):
        entry = Gtk.Entry()
        entry.set_placeholder_text("Profile name")
        dlg = Adw.AlertDialog.new("Save profile", "Stores the current output, filter and encoder settings.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")

        def on_response(d, r):
            if r != "save":
                return
            name = entry.get_text().strip()
            if not name:
                return
            cur = self._gather()
            self.cfg.profiles[name] = {k: getattr(cur, k) for k in PROFILE_KEYS}
            save_config(self._gather())
            self._refresh_profiles(select=f"★ {name}")
            self._toast(f"Saved profile: {name}")
        dlg.connect("response", on_response)
        dlg.present(self)

    def _delete_profile(self):
        label = self._selected_profile_label()
        if not label.startswith("★ "):
            return
        name = label[2:]
        self.cfg.profiles.pop(name, None)
        save_config(self._gather())
        self._refresh_profiles()
        self._toast(f"Deleted profile: {name}")

    # ------------------------------------------------------------------
    # File pickers, queue
    # ------------------------------------------------------------------
    def _pick_folder(self, title, setter):
        d = Gtk.FileDialog()
        d.set_title(title)

        def done(dlg, res):
            try:
                f = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                setter(f.get_path())
        d.select_folder(self, None, done)

    def _pick_file(self, title, setter, patterns=None):
        d = Gtk.FileDialog()
        d.set_title(title)
        if patterns:
            f = Gtk.FileFilter()
            f.set_name(patterns[0])
            for p in patterns[1:]:
                f.add_pattern(p)
            store = Gio.ListStore.new(Gtk.FileFilter)
            store.append(f)
            d.set_filters(store)

        def done(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                setter(f.get_path())
        d.open(self, None, done)

    def _make_env_popover(self, btn):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for m in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{m}")(6)
        cands = find_pythons(self.apisr_row.get_text().strip())
        if not cands:
            box.append(Gtk.Label(label="No environments found"))
        for cand in cands:
            b = Gtk.Button(label=cand)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, p=cand, po=pop: (self.python_row.set_text(p), po.popdown()))
            box.append(b)
        pop.set_child(box)
        btn.set_popover(pop)

    def _add_paths(self, paths):
        for p in paths:
            if p and p not in self.videos:
                self.videos.append(p)
        self._rebuild_queue()

    def _on_drop(self, target, value, x, y):
        self._add_paths([f.get_path() for f in value.get_files() if f.get_path()])
        return True

    def _add_videos(self):
        d = Gtk.FileDialog()
        d.set_title("Select video(s)")
        f = Gtk.FileFilter()
        f.set_name("Video files")
        f.add_mime_type("video/*")
        for ext in ("mkv", "mp4", "avi", "mov", "webm", "m2ts", "ts", "flv", "wmv"):
            f.add_pattern(f"*.{ext}")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        d.set_filters(store)

        def done(dlg, res):
            try:
                files = dlg.open_multiple_finish(res)
            except GLib.Error:
                return
            self._add_paths([files.get_item(i).get_path() for i in range(files.get_n_items())])
        d.open_multiple(self, None, done)

    def _rebuild_queue(self):
        for r in self.queue_rows:
            self.queue_group.remove(r)
        self.queue_rows = []
        if not self.videos:
            r = Adw.ActionRow()
            r.set_title("No videos in queue")
            r.set_subtitle("Drag and drop anime video files here, or click 'Add videos'")
            logo_tex = get_logo_texture()
            if logo_tex:
                img = Gtk.Image.new_from_paintable(logo_tex)
                img.set_pixel_size(32)
                r.add_prefix(img)
            else:
                r.add_prefix(Gtk.Image.new_from_icon_name(pick_icon(*ICON_VIDEOS_EMPTY)))
            self.queue_group.add(r)
            self.queue_rows.append(r)
            return
        last = len(self.videos) - 1
        for i, path in enumerate(self.videos):
            r = Adw.ActionRow()
            r.set_title(esc(Path(path).name))
            r.set_subtitle(esc(str(Path(path).parent)))
            r.set_subtitle_lines(1)
            r.add_prefix(Gtk.Image.new_from_icon_name(pick_icon(*ICON_VIDEO)))
            up = icon_button(ICON_UP, "Move up", lambda p=path: self._move_video(p, -1))
            up.set_sensitive(i > 0)
            down = icon_button(ICON_DOWN, "Move down", lambda p=path: self._move_video(p, 1))
            down.set_sensitive(i < last)
            r.add_suffix(up)
            r.add_suffix(down)
            r.add_suffix(icon_button(ICON_TRASH, "Remove from queue", lambda p=path: self._remove_video(p)))
            self.queue_group.add(r)
            self.queue_rows.append(r)

    def _move_video(self, path, delta):
        if path not in self.videos:
            return
        i = self.videos.index(path)
        j = i + delta
        if 0 <= j < len(self.videos):
            self.videos[i], self.videos[j] = self.videos[j], self.videos[i]
            self._rebuild_queue()

    def _remove_video(self, path):
        if path in self.videos:
            self.videos.remove(path)
        self._rebuild_queue()

    def _clear_queue(self):
        if self.worker and self.worker.is_alive():
            return self._toast("Can't clear the queue while processing")
        self.videos.clear()
        self._rebuild_queue()

    # ------------------------------------------------------------------
    # Weights
    # ------------------------------------------------------------------
    def _refresh_weights(self):
        apisr = self.apisr_row.get_text().strip() if hasattr(self, "apisr_row") else self.cfg.apisr_dir
        pre = Path(apisr) / "pretrained"
        current = ""
        if hasattr(self, "weight_row"):
            current = self.weight_map.get(combo_value(self.weight_row), "")
        if not current:
            w = self.cfg.weight
            current = w if os.path.isabs(w) else (str(pre / w) if w else "")
        wm = {}
        if pre.is_dir():
            for p in sorted(pre.glob("*.pth")):
                wm[p.name] = str(p)
        if current and current not in wm.values() and os.path.exists(current):
            wm[f"{Path(current).name}  ·  {Path(current).parent}"] = current
        self.weight_map = wm
        sel = next((k for k, v in wm.items() if v == current), None)
        if sel is None and wm:
            sel = next((k for k in wm if k.startswith("2x")), next(iter(wm)))
        combo_set(self.weight_row, list(wm), sel)

    def _pick_weight(self):
        def got(path):
            self.cfg.weight = path
            self.weight_map = {}
            self._refresh_weights()
        self._pick_file("Select model weights", got, ["PyTorch weights", "*.pth", "*.pt"])

    # ------------------------------------------------------------------
    # Preview, test frame
    # ------------------------------------------------------------------
    def _on_preview(self, payload):
        old_path, new_path = payload
        try:
            self.split_area.set_images(old_path, new_path)
            tex_old = load_image_texture(old_path)
            if tex_old:
                self.pic_old.set_paintable(tex_old)
            tex_new = load_image_texture(new_path)
            if tex_new:
                self.pic_new.set_paintable(tex_new)
        except Exception as e:
            self._append(f"Preview display error: {e}")

    def _preflight_checks(self, cfg, need_videos=True):
        """Common validation. Returns True when the config is runnable."""
        if need_videos and not cfg.videos:
            self._alert("No videos", "Add at least one video to the queue first.")
            return False
        if not (Path(cfg.apisr_dir) / "test_code" / "inference.py").exists():
            self._alert("APISR folder", "The APISR folder must contain test_code/inference.py.")
            return False
        if not cfg.weight or not Path(cfg.weight).exists():
            self._alert("Model weights", "Model weights not found. Use 'Download models' or pick a .pth file.")
            return False
        if not os.path.exists(cfg.python):
            self._alert("Python", f"Python not found: {cfg.python}")
            return False
        problems = validate_cfg(cfg)
        if problems:
            self._alert("Check your settings", "\n\n".join(problems))
            return False
        return True

    def _test_single_frame(self):
        cfg = self._gather()
        if not self._preflight_checks(cfg):
            return
        self.test_1frame_btn.set_sensitive(False)
        self._toast("Extracting and upscaling 1 test frame...")

        def work():
            try:
                src, res = render_test_frame(cfg, cfg.videos[0], lambda m: self.q.put(("log", m)))
                self.q.put(("preview", (src, res)))
                self.q.put(("log", f"Test frame rendered successfully: {Path(res).name}"))
                if cfg.sound_cues:
                    self.q.put(("sound", "complete"))
            except Exception as e:
                self.q.put(("log", f"Test frame error: {e}"))
            finally:
                GLib.idle_add(lambda: self.test_1frame_btn.set_sensitive(True))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # Model downloader (verified)
    # ------------------------------------------------------------------
    def _download_models(self):
        apisr = self.apisr_row.get_text().strip()
        if not apisr or not Path(apisr).is_dir():
            return self._alert("APISR folder", "Set the APISR folder first; weights are saved to its pretrained/ folder.")
        pre = Path(apisr) / "pretrained"
        self._toast("Downloading models in background...")

        def work():
            jobs = []
            for name, url in APISR_WEIGHTS:
                tag = re.search(r"/download/([^/]+)/", url).group(1)
                jobs.append((name, url, pre, ("gh", tag)))
            for name in ARNNDN_FILES:
                url = f"https://raw.githubusercontent.com/{ARNNDN_REPO}/master/{name}"
                jobs.append((name, url, MODELS_CACHE_DIR, ("blob", name)))
            for name, url, target, how in jobs:
                dest = target / name
                if dest.exists() and dest.stat().st_size >= 1024:
                    self.q.put(("log", f"Model already present: {name}"))
                    continue
                self.q.put(("log", f"Downloading {name}..."))
                sha256 = sha1 = None
                if how[0] == "gh":
                    sha256, _ = github_expected_sha256("Kiteretsu77", "APISR", how[1], name)
                else:
                    sha1 = github_blob_sha1(*ARNNDN_REPO.split("/"), how[1])
                ok, msg = download_verified(
                    url, dest, sha256, sha1,
                    progress=lambda f, n=name: self.q.put(("dlprogress", (n, f))))
                if ok:
                    self.q.put(("log", f"Saved: {dest} ({dest.stat().st_size / 1e6:.1f} MB, {msg})"))
                else:
                    self.q.put(("log", f"Failed downloading {name}: {msg}"))
            self.q.put(("dlprogress", None))
            self.q.put(("log", "All model checks and downloads completed."))
            GLib.idle_add(self._refresh_weights)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------
    def _set_running(self, running):
        self.start_btn.set_sensitive(not running)
        self.cancel_btn.set_sensitive(running)
        self.test_1frame_btn.set_sensitive(not running)
        self.sidebar_box.set_sensitive(not running)
        if running:
            self._inhibit_sleep("Upscaling video with APISR")
        else:
            self._uninhibit_sleep()

    def _start(self):
        cfg = self._gather()
        if not self._preflight_checks(cfg):
            return
        save_config(cfg)
        if not cfg.confirm_start:
            return self._begin(cfg)
        self._toast("Estimating disk use and time...")

        def work():
            try:
                s = plan_summary(cfg)
            except Exception as e:
                s = {"error": str(e)}
            GLib.idle_add(self._show_summary, cfg, s)
        threading.Thread(target=work, daemon=True).start()

    def _show_summary(self, cfg, s):
        if "error" in s:
            return self._begin(cfg)
        lines = []
        for r in s["rows"][:6]:
            if "error" in r:
                lines.append(f"• {r['name']}: could not read ({r['error'][:60]})")
            else:
                lines.append(f"• {r['name']}: {r['src']} @ {r['fps']:.2f} fps, ~{r['frames']:,} frames")
        if len(s["rows"]) > 6:
            lines.append(f"• …and {len(s['rows']) - 6} more")
        lines.append("")
        lines.append(f"Temporary disk (peak): ~{s['peak_bytes'] / 1e9:.1f} GB "
                     f"(free: {s['free_bytes'] / 1e9:.0f} GB on {s['work']})")
        if s["eta_seconds"]:
            lines.append(f"Estimated time: ~{fmt_time(s['eta_seconds'])} "
                         f"(from your last run at {s['bench_fps']:.1f} fps on this model)")
        else:
            lines.append("Estimated time: measured after the first chunk (no earlier run on this model).")
        tight = s["peak_bytes"] > s["free_bytes"] * 0.9
        if tight:
            lines.append("\n⚠ Free space is below the estimated peak. Lower the chunk size or pick another frames folder.")
        dlg = Adw.AlertDialog.new("Ready to start?", "\n".join(lines))
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("start", "Start anyway" if tight else "Start")
        dlg.set_response_appearance("start", Adw.ResponseAppearance.DESTRUCTIVE if tight
                                    else Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("cancel" if tight else "start")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda d, r: self._begin(cfg) if r == "start" else None)
        dlg.present(self)

    def _begin(self, cfg):
        self.run_cfg = cfg
        self.pipeline = Pipeline(cfg, lambda k, p: self.q.put((k, p)))
        self._set_running(True)
        self.bar.set_fraction(0)
        self.title_label.set_label("Starting…")
        self.extra_label.set_label("")
        self._append(f"Starting. Weights: {cfg.weight}")

        def work():
            pipe = self.pipeline
            try:
                pipe.preflight()
                vids = cfg.videos
                for i, v in enumerate(vids, 1):
                    pipe.process(v, i, len(vids))
                self.q.put(("done", ("ok", "All videos finished.")))
            except Cancelled:
                self.q.put(("done", ("cancelled",
                                     "Cancelled. Finished chunks were kept, so you can resume "
                                     "by pressing Start again with the same settings.")))
            except Exception as e:
                self.q.put(("log", traceback.format_exc()))
                self.q.put(("done", ("error", str(e))))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _cancel(self):
        if self.pipeline:
            self.title_label.set_label("Cancelling…")
            self.pipeline.cancel()

    def _finished(self, kind, msg):
        self._set_running(False)
        self.tile_reset()
        self._append("\n" + msg)
        cfg = self.run_cfg or self.cfg  # settings the run actually used
        if kind == "ok":
            self.bar.set_fraction(1.0)
            self.title_label.set_label("Finished")
            self.extra_label.set_label(self.last_output or msg)
            self._toast("Upscaling finished", "win.open-output", "Show file")
            if cfg.sound_cues:
                self._play_cue("complete")
            on_done = cfg.on_done
            if on_done == "Desktop notification":
                self._notify("APISR Upscaler", "All videos finished successfully!")
            elif on_done == "Suspend PC":
                subprocess.run(["systemctl", "suspend"])
            elif on_done == "Shutdown PC":
                subprocess.run(["systemctl", "poweroff"])
        elif kind == "cancelled":
            self.bar.set_fraction(0.0)
            self.title_label.set_label("Cancelled")
            self.extra_label.set_label("Press Start to resume where you left off.")
        else:
            self.bar.set_fraction(0.0)
            self.title_label.set_label("Something went wrong")
            self.extra_label.set_label("See the message and the log for details.")
            self._alert("Something went wrong", msg)
        save_config(self._gather())

    def _play_cue(self, cue_name):
        def _play():
            for cmd in (["canberra-gtk-play", "-i", cue_name],
                        ["paplay", f"/usr/share/sounds/freedesktop/stereo/{cue_name}.oga"],
                        ["pw-play", f"/usr/share/sounds/freedesktop/stereo/{cue_name}.oga"]):
                if shutil.which(cmd[0]):
                    try:
                        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
                        return
                    except Exception:
                        pass
        threading.Thread(target=_play, daemon=True).start()

    def tile_reset(self):
        for v in (self.v_speed, self.v_eta, self.v_gpu, self.v_disk):
            v.set_label("—")

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "progress":
                    self._on_progress(payload)
                elif kind == "preview":
                    self._on_preview(payload)
                elif kind == "workdir":
                    self.last_work = payload
                elif kind == "output":
                    self.last_output = payload
                elif kind == "devices":
                    self._on_devices(payload)
                elif kind == "missing":
                    self._on_missing(payload)
                elif kind == "bench":
                    key, fps = payload
                    old = self.cfg.bench.get(key)
                    self.cfg.bench[key] = round(fps if not old else 0.5 * old + 0.5 * fps, 3)
                elif kind == "sound":
                    self._play_cue(payload)
                elif kind == "dlprogress":
                    if payload is None:
                        self.bar.set_fraction(0)
                        self.title_label.set_label("Ready to upscale")
                    else:
                        self.title_label.set_label(f"Downloading {payload[0]}")
                        self.bar.set_fraction(payload[1])
                elif kind == "done":
                    self._finished(*payload)
        except queue.Empty:
            pass
        return True

    def _on_progress(self, p):
        self.title_label.set_label(p["title"])
        if p["pct"] is None:
            self.bar.set_pulse(True)
        else:
            self.bar.set_fraction(p["pct"] / 100.0)
        self.extra_label.set_label(p.get("extra") or "")
        self.v_speed.set_label(f"{p['fps']:.2f} fps" if p.get("fps") else "—")
        self.v_eta.set_label(p.get("eta") or "—")
        self.v_gpu.set_label(p.get("gpu") or "—")
        self.v_disk.set_label(f"{p['disk']:.0f} GB" if p.get("disk") else "—")

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def _detect_devices(self):
        py = self.python_row.get_text().strip() or sys.executable
        self.device_info.set_subtitle("Detecting (importing PyTorch, may take a few seconds)…")

        def work():
            env = os.environ.copy()
            for k in ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES"):
                env.pop(k, None)
            setup_nvrtc_env(py, env)
            self.q.put(("devices", detect_torch(py, env)))
        threading.Thread(target=work, daemon=True).start()

    def _on_devices(self, info):
        if "error" in info:
            self.device_info.set_subtitle(esc(info["error"].splitlines()[0]))
            self._append("Device detection: " + info["error"])
            return
        kind = (f"ROCm {info['hip']}" if info.get("hip") else
                f"CUDA {info['cuda']}" if info.get("cuda") else "CPU-only")
        summary = f"PyTorch {info.get('torch', 'unknown')} ({kind} build)."

        values = [DEVICE_AUTO]
        for d in info.get("devices", []):
            values.append(f"GPU {d['index']}: {d['name']} ({d['mem_gb']} GB)")
        values.append(DEVICE_CPU)
        current = combo_value(self.device_row)
        pick = current if current in values else None
        if pick is None:
            m = re.match(r"GPU (\d+)", current)
            pick = next((v for v in values if m and v.startswith(f"GPU {m.group(1)}:")), None)
        if pick is None:
            pick = values[1] if info.get("devices") else DEVICE_AUTO
        combo_set(self.device_row, values, pick)

        notes = []
        hint = torch_install_hint(info.get("devices"))
        if not info.get("available", False):
            if not info.get("cuda") and not info.get("hip"):
                notes.append("This is a CPU-only PyTorch build. Reinstall a GPU build in the APISR env: " + hint)
            else:
                notes.append("PyTorch has GPU support but can't see a GPU. Verify 'nvidia-smi' runs in a terminal.")
        else:
            archs = info.get("arch_list", [])
            if archs and not info.get("hip"):
                for d in info.get("devices", []):
                    if not arch_ok(archs, d.get("major", 0), d.get("minor", 0)):
                        notes.append(f"GPU {d['index']} (sm_{d['major']}{d['minor']}) isn't covered by this PyTorch "
                                     f"build. Install one that matches: {hint}")
            if not notes:
                notes.append("GPU is available - select it above.")
        text = summary + " " + " ".join(notes)
        self.device_info.set_subtitle(esc(text))
        self._append("Device detection: " + text)

    def _show_help(self):
        apisr = self.apisr_row.get_text().strip()
        if not (Path(apisr) / "test_code" / "inference.py").exists():
            return self._alert("APISR folder", "Set APISR folder first.")
        py = self.python_row.get_text().strip() or sys.executable

        def work():
            env = os.environ.copy()
            env["PYTHONPATH"] = apisr + os.pathsep + env.get("PYTHONPATH", "")
            setup_nvrtc_env(py, env)
            try:
                r = subprocess.run([py, "test_code/inference.py", "--help"], cwd=apisr, env=env,
                                   capture_output=True, text=True, timeout=180)
                self.q.put(("log", "\n$ inference.py --help\n" + (r.stdout + r.stderr)))
            except Exception as e:
                self.q.put(("log", f"Could not run --help: {e}"))
        self._append("Running inference.py --help...")
        threading.Thread(target=work, daemon=True).start()

    def _check_packages(self):
        apisr = self.apisr_row.get_text().strip()
        py = self.python_row.get_text().strip() or sys.executable
        if not (Path(apisr) / "test_code" / "inference.py").exists():
            return self._alert("APISR folder", "Set APISR folder first.")
        if not os.path.exists(py):
            return self._alert("Python", f"Python not found: {py}")
        self._append(f"Scanning what APISR imports and checking {py} ...")

        def work():
            names = [n for n in local_import_closure(apisr) if n.lower() not in GUI_MODULES]
            missing, err = find_missing_modules(py, names)
            self.q.put(("missing", (py, missing, err)))
        threading.Thread(target=work, daemon=True).start()

    def _on_missing(self, payload):
        py, missing, err = payload
        if err:
            return self._append("Package check failed: " + err)
        if not missing:
            self._append("All packages that APISR imports are installed in that environment.")
            return self._toast("Everything APISR imports is installed")
        self._append("Missing in that environment: " + ", ".join(missing))
        torchy = [m for m in missing if m in NO_AUTO_INSTALL]
        pip_pkgs = [PIP_NAMES.get(m, m) for m in missing if m not in NO_AUTO_INSTALL]
        if torchy:
            self._append("torch/torchvision missing. Install the GPU build that matches your card:\n"
                         f"  {py} -m {torch_install_hint()}")
        if not pip_pkgs:
            return
        dlg = Adw.AlertDialog.new("Install missing packages?",
                                  "These will be installed with pip:\n\n" + ", ".join(pip_pkgs))
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("install", "Install")
        dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("install")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda d, r: threading.Thread(
            target=self._pip_install, args=(py, pip_pkgs), daemon=True).start() if r == "install" else None)
        dlg.present(self)

    def _pip_install(self, py, pkgs):
        env = os.environ.copy()
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        cmd = [py, "-m", "pip", "install"] + pkgs
        self.q.put(("log", "$ " + " ".join(shlex.quote(c) for c in cmd)))
        try:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.q.put(("log", line))
            rc = proc.wait()
        except Exception as e:
            self.q.put(("log", f"pip failed to start: {e}"))
            return
        self.q.put(("log", "Install complete." if rc == 0 else f"pip failed with code {rc}"))

    def _setup_help(self):
        hint = torch_install_hint().replace("pip install", "uv pip install", 1)
        self._append(f"""
--- Setting up a Python environment for APISR ---
(Fedora's system Python only runs this GUI; APISR gets its own environment.)
uv venv --python 3.10 .venv
source .venv/bin/activate
{hint}
uv pip install -r requirements.txt
Optional, for low-overhead GPU stats in this app (system Python):
pip install --user nvidia-ml-py
""")

    def _copy_commands(self):
        cfg = self._gather()
        if not cfg.videos:
            return self._alert("No videos", "Add a video to the queue first.")
        if not cfg.weight:
            return self._alert("Model weights", "Select model weights first.")
        problems = validate_cfg(cfg)
        if problems:
            return self._alert("Check your settings", "\n\n".join(problems))

        def work():
            try:
                text = describe_commands(cfg, cfg.videos[0])
                GLib.idle_add(self._set_clipboard, text)
            except Exception as e:
                self.q.put(("log", f"Could not build command lines: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _set_clipboard(self, text):
        try:
            self.get_clipboard().set(text)
            self._toast("Command lines copied to clipboard")
        except Exception as e:
            self._append(f"Clipboard error: {e}")
        return False

    def _open_path(self, target):
        if not target or not os.path.exists(target):
            return self._toast("That location doesn't exist yet")
        try:
            subprocess.Popen(["xdg-open", target])
        except Exception as e:
            self._alert("Open", str(e))

    def _open_frames(self):
        target = (self.last_work or self.work_row.get_text().strip() or self.out_row.get_text().strip())
        if not target and self.videos:
            target = str(Path(self.videos[0]).parent)
        self._open_path(target)

    def _open_output(self):
        self._open_path(str(Path(self.last_output).parent) if self.last_output else "")

    def _about(self):
        try:
            dlg = Adw.AboutDialog.new()
        except AttributeError:
            return self._toast("APISR Upscaler")
        dlg.set_application_name("APISR Upscaler")
        dlg.set_version(__version__)
        dlg.set_developer_name("A front-end for APISR (CVPR 2024)")
        dlg.set_comments("Anime super-resolution, denoising and remastering suite.")
        dlg.set_website("https://github.com/Kiteretsu77/APISR")
        logo_tex = get_logo_texture()
        if logo_tex and hasattr(dlg, "set_logo"):
            dlg.set_logo(logo_tex)
        else:
            dlg.set_application_icon(APP_ID)
        dlg.present(self)

    def _on_close(self, *_):
        if self.worker and self.worker.is_alive():
            dlg = Adw.AlertDialog.new(
                "Quit while processing?",
                "Processing is still running. Cancel it and quit? Finished chunks are kept.")
            dlg.add_response("keep", "Keep running")
            dlg.add_response("quit", "Cancel and quit")
            dlg.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("keep")
            dlg.set_close_response("keep")
            dlg.connect("response", self._on_quit_response)
            dlg.present(self)
            return True
        save_config(self._gather())
        return False

    def _on_quit_response(self, dlg, response):
        if response == "quit":
            if self.pipeline:
                self.pipeline.cancel()
            self._uninhibit_sleep()
            save_config(self._gather())
            self.destroy()


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        present = Gio.SimpleAction.new("present", None)
        present.connect("activate", lambda *_: self.do_activate())
        self.add_action(present)

    def do_activate(self):
        win = self.props.active_window or MainWindow(self)
        win.present()


def main(launcher_path=None):
    ensure_desktop_integration(LOGO_SVG, Path(launcher_path or sys.argv[0]).resolve())
    Adw.init()
    return Application().run(sys.argv[:1])
