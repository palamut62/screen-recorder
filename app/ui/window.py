from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path
from time import monotonic

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from app.core.state import AppState, Region
from app.recorder.ffmpeg import FFmpegRecorder, RecorderError
from app.utils.env import detect_session_type

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".gif", ".mov", ".avi"}
_CSS_PATH = Path(__file__).parent / "style.css"


# ─────────────────────────────────────────────────────────────
#  Floating toolbar (shown during recording)
# ─────────────────────────────────────────────────────────────

class FloatingControlsWindow(Gtk.Window):
    def __init__(self, parent: "RecorderWindow") -> None:
        super().__init__(
            application=parent.get_application(),
            transient_for=parent,
            decorated=False,
        )
        self.parent_window = parent
        self.set_title("Recorder Controls")
        self.set_resizable(False)
        self.set_default_size(340, 64)

        handle = Gtk.WindowHandle()
        self.set_child(handle)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.add_css_class("floating-toolbar")
        handle.set_child(bar)

        # Left: dot + status + timer
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        info.set_hexpand(True)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self._dot = Gtk.Label(label="●")
        self._dot.add_css_class("toolbar-dot")
        self._status = Gtk.Label(label="Recording")
        self._status.add_css_class("toolbar-status")
        self._status.set_xalign(0)
        top_row.append(self._dot)
        top_row.append(self._status)

        self._timer = Gtk.Label(label="00:00")
        self._timer.add_css_class("toolbar-timer")
        self._timer.set_xalign(0)

        info.append(top_row)
        info.append(self._timer)

        stop_btn = Gtk.Button(label="■  Stop")
        stop_btn.add_css_class("btn-danger")
        stop_btn.connect("clicked", lambda _: self.parent_window.stop_recording_from_toolbar())

        self._toggle_btn = Gtk.Button(label="Show")
        self._toggle_btn.add_css_class("btn-secondary")
        self._toggle_btn.connect("clicked", lambda _: self.parent_window.toggle_main_window())

        bar.append(info)
        bar.append(stop_btn)
        bar.append(self._toggle_btn)

    def sync_toggle(self, visible: bool) -> None:
        self._toggle_btn.set_label("Hide" if visible else "Show")

    def update(self, elapsed: str, recording: bool) -> None:
        self._status.set_text("Recording" if recording else "Stopped")
        self._timer.set_text(elapsed)


# ─────────────────────────────────────────────────────────────
#  Library row
# ─────────────────────────────────────────────────────────────

class FileRow(Gtk.ListBoxRow):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("lib-row-box")
        box.set_margin_top(1)
        box.set_margin_bottom(1)
        box.set_margin_start(2)
        box.set_margin_end(2)

        icon_name = "image-x-generic-symbolic" if path.suffix.lower() == ".gif" else "video-x-generic-symbolic"
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        icon.set_opacity(0.55)

        name = Gtk.Label(label=path.name, xalign=0)
        name.add_css_class("lib-name")
        name.set_hexpand(True)
        name.set_ellipsize(3)

        ext = Gtk.Label(label=path.suffix.lstrip(".").upper())
        ext.add_css_class("lib-ext")

        box.append(icon)
        box.append(name)
        box.append(ext)
        self.set_child(box)


# ─────────────────────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────────────────────

class RecorderWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._log = logging.getLogger(__name__)
        self.state = AppState()
        self.recorder = FFmpegRecorder()
        self._toolbar: FloatingControlsWindow | None = None
        self._rec_start: float | None = None
        self._timer_id: int | None = None
        self._toolbar_tid: int | None = None
        self.media_path: Path | None = None

        self.set_title("Screen Recorder")
        self.set_default_size(860, 520)
        self._load_css()
        self.set_content(self._build_ui())
        self._refresh_library()
        self._refresh_ui()
        self._log.info("Window ready")

    # ── CSS ─────────────────────────────────────────────────

    def _load_css(self) -> None:
        p = Gtk.CssProvider()
        p.load_from_path(str(_CSS_PATH))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), p,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # ── Root layout ──────────────────────────────────────────

    def _build_ui(self) -> Gtk.Widget:
        # Top-level: ToolbarView with HeaderBar
        tv = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.add_css_class("flat")

        # Left of header: session chip + rec chip
        left_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._session_chip = Gtk.Label(label=detect_session_type().upper())
        self._session_chip.add_css_class("session-chip")
        self._rec_chip = Gtk.Label(label="● REC")
        self._rec_chip.add_css_class("rec-chip")
        self._rec_chip.set_visible(False)
        left_box.append(self._session_chip)
        left_box.append(self._rec_chip)
        header.pack_start(left_box)

        tv.add_top_bar(header)

        # Body: horizontal split
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.add_css_class("window-bg")

        # Left panel (controls)
        left = self._build_left_panel()
        left.set_size_request(340, -1)
        left.add_css_class("side-panel")

        # Right panel (library)
        right = self._build_right_panel()
        right.set_hexpand(True)

        body.append(left)
        body.append(right)
        tv.set_content(body)
        return tv

    # ── Left panel ───────────────────────────────────────────

    def _build_left_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Tab switcher
        self._stack = Adw.ViewStack()
        self._stack.add_titled(self._build_capture_tab(), "capture", "Capture")
        self._stack.add_titled(self._build_media_tab(), "media", "Media Tools")
        self._stack.set_vexpand(True)

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)

        sw_wrap = Gtk.Box()
        sw_wrap.set_margin_top(8)
        sw_wrap.set_margin_bottom(6)
        sw_wrap.set_margin_start(10)
        sw_wrap.set_margin_end(10)
        sw_wrap.append(switcher)

        box.append(sw_wrap)
        box.append(self._sep())
        box.append(self._stack)
        box.append(self._build_status_bar())
        return box

    # ── Capture tab ──────────────────────────────────────────

    def _build_capture_tab(self) -> Gtk.Widget:
        tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        tab.set_margin_top(12)
        tab.set_margin_bottom(12)
        tab.set_margin_start(12)
        tab.set_margin_end(12)

        # Record mode buttons
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._fs_btn = Gtk.Button()
        self._fs_btn.set_hexpand(True)
        self._icon_btn(self._fs_btn, "view-fullscreen-symbolic", "Full Screen")
        self._fs_btn.add_css_class("btn-primary")
        self._fs_btn.connect("clicked", self._on_fullscreen)

        self._region_btn = Gtk.Button()
        self._region_btn.set_hexpand(True)
        self._icon_btn(self._region_btn, "object-select-symbolic", "Select Region")
        self._region_btn.add_css_class("btn-secondary")
        self._region_btn.connect("clicked", self._on_select_region)

        mode_box.append(self._fs_btn)
        mode_box.append(self._region_btn)
        tab.append(mode_box)

        # Region info
        self._region_info = Gtk.Label(label="Full screen", xalign=0)
        self._region_info.add_css_class("region-info")
        tab.append(self._region_info)

        # Settings rows (grouped)
        settings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)

        # Format row
        fmt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fmt_row.add_css_class("row-box")
        fmt_lbl = Gtk.Label(label="Format", xalign=0)
        fmt_lbl.add_css_class("row-label")
        fmt_lbl.set_hexpand(True)
        self._fmt_drop = Gtk.DropDown(model=Gtk.StringList.new(["mp4", "mkv", "webm"]))
        self._fmt_drop.connect("notify::selected-item", self._on_format_changed)
        fmt_row.append(fmt_lbl)
        fmt_row.append(self._fmt_drop)

        # FPS row
        fps_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fps_row.add_css_class("row-box")
        fps_lbl = Gtk.Label(label="FPS", xalign=0)
        fps_lbl.add_css_class("row-label")
        fps_lbl.set_hexpand(True)
        self._fps_drop = Gtk.DropDown(model=Gtk.StringList.new(["30", "60"]))
        self._fps_drop.connect("notify::selected-item", self._on_fps_changed)
        fps_row.append(fps_lbl)
        fps_row.append(self._fps_drop)

        # Folder row
        folder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_row.add_css_class("row-box")
        folder_icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        folder_icon.set_pixel_size(15)
        folder_icon.set_opacity(0.55)
        self._folder_lbl = Gtk.Label(xalign=0)
        self._folder_lbl.add_css_class("row-label")
        self._folder_lbl.set_hexpand(True)
        self._folder_lbl.set_ellipsize(3)
        chg_btn = Gtk.Button(label="Change")
        chg_btn.add_css_class("btn-secondary")
        chg_btn.connect("clicked", self._on_choose_folder)
        folder_row.append(folder_icon)
        folder_row.append(self._folder_lbl)
        folder_row.append(chg_btn)

        settings.append(fmt_row)
        settings.append(fps_row)
        settings.append(folder_row)
        tab.append(settings)

        # Action buttons
        act_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._stop_btn = Gtk.Button()
        self._stop_btn.set_hexpand(True)
        self._icon_btn(self._stop_btn, "media-playback-stop-symbolic", "Stop")
        self._stop_btn.add_css_class("btn-danger")
        self._stop_btn.connect("clicked", self._on_stop)

        self._open_folder_btn = Gtk.Button()
        self._icon_btn(self._open_folder_btn, "folder-open-symbolic", "Folder")
        self._open_folder_btn.add_css_class("btn-secondary")
        self._open_folder_btn.connect("clicked", self._on_show_folder)

        act_box.append(self._stop_btn)
        act_box.append(self._open_folder_btn)
        tab.append(act_box)

        return tab

    # ── Media Tools tab ──────────────────────────────────────

    def _build_media_tab(self) -> Gtk.Widget:
        tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        tab.set_margin_top(12)
        tab.set_margin_bottom(12)
        tab.set_margin_start(12)
        tab.set_margin_end(12)

        # Source buttons
        src_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        open_btn = Gtk.Button()
        open_btn.set_hexpand(True)
        self._icon_btn(open_btn, "document-open-symbolic", "Open File")
        open_btn.add_css_class("btn-secondary")
        open_btn.connect("clicked", self._on_choose_media)

        self._use_sel_btn = Gtk.Button()
        self._use_sel_btn.set_hexpand(True)
        self._icon_btn(self._use_sel_btn, "go-next-symbolic", "Use Selection")
        self._use_sel_btn.add_css_class("btn-secondary")
        self._use_sel_btn.connect("clicked", self._on_use_selection)

        src_box.append(open_btn)
        src_box.append(self._use_sel_btn)
        tab.append(src_box)

        # File info card
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        info_box.add_css_class("panel-card")
        info_box.set_margin_top(0)

        self._media_name = Gtk.Label(label="No file selected", xalign=0)
        self._media_name.add_css_class("lib-name")
        self._media_name.set_ellipsize(3)

        self._media_meta = Gtk.Label(label="Open a file or select from library →", xalign=0)
        self._media_meta.add_css_class("detail-meta")
        self._media_meta.set_wrap(True)

        info_box.set_margin_top(8)
        info_box.set_margin_bottom(8)
        info_box.set_margin_start(10)
        info_box.set_margin_end(10)
        info_box.append(self._media_name)
        info_box.append(self._media_meta)
        tab.append(info_box)

        # Export options
        opts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)

        exp_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        exp_row.add_css_class("row-box")
        exp_lbl = Gtk.Label(label="Export As", xalign=0)
        exp_lbl.add_css_class("row-label")
        exp_lbl.set_hexpand(True)
        self._exp_fmt = Gtk.DropDown(model=Gtk.StringList.new(["mp4", "mkv", "webm", "gif"]))
        exp_row.append(exp_lbl)
        exp_row.append(self._exp_fmt)

        spd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spd_row.add_css_class("row-box")
        spd_lbl = Gtk.Label(label="Speed", xalign=0)
        spd_lbl.add_css_class("row-label")
        spd_lbl.set_hexpand(True)
        self._exp_spd = Gtk.DropDown(model=Gtk.StringList.new(["0.5x", "1.0x", "1.5x", "2.0x", "4.0x"]))
        self._exp_spd.set_selected(1)
        spd_row.append(spd_lbl)
        spd_row.append(self._exp_spd)

        opts.append(exp_row)
        opts.append(spd_row)
        tab.append(opts)

        # Export actions
        exp_act = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._play_btn = Gtk.Button()
        self._icon_btn(self._play_btn, "media-playback-start-symbolic", "Play")
        self._play_btn.add_css_class("btn-secondary")
        self._play_btn.connect("clicked", self._on_play_media)

        self._export_btn = Gtk.Button()
        self._export_btn.set_hexpand(True)
        self._icon_btn(self._export_btn, "document-save-symbolic", "Export")
        self._export_btn.add_css_class("btn-primary")
        self._export_btn.connect("clicked", self._on_export)

        exp_act.append(self._play_btn)
        exp_act.append(self._export_btn)
        tab.append(exp_act)

        # Export status
        exp_status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._exp_spinner = Gtk.Spinner()
        self._exp_spinner.set_visible(False)
        self._exp_status = Gtk.Label(label="Ready", xalign=0)
        self._exp_status.add_css_class("status-text")
        self._exp_status.set_hexpand(True)
        exp_status.append(self._exp_spinner)
        exp_status.append(self._exp_status)
        tab.append(exp_status)

        return tab

    # ── Right panel (library) ────────────────────────────────

    def _build_right_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Header
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hdr.set_margin_top(10)
        hdr.set_margin_bottom(8)
        hdr.set_margin_start(14)
        hdr.set_margin_end(10)

        hdr_lbl = Gtk.Label(label="Recordings", xalign=0)
        hdr_lbl.add_css_class("lib-name")
        hdr_lbl.set_hexpand(True)

        ref_btn = Gtk.Button()
        ref_btn.add_css_class("btn-secondary")
        img = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        img.set_pixel_size(15)
        ref_btn.set_child(img)
        ref_btn.connect("clicked", lambda _: (self._refresh_library(), self._refresh_ui()))

        hdr.append(hdr_lbl)
        hdr.append(ref_btn)
        box.append(hdr)
        box.append(self._sep())

        # Scrollable list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._lib = Gtk.ListBox()
        self._lib.add_css_class("lib-list")
        self._lib.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._lib.connect("row-selected", self._on_row_selected)
        scroll.set_child(self._lib)
        box.append(scroll)

        # Detail strip
        det = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        det.add_css_class("detail-strip")

        self._det_name = Gtk.Label(label="No file selected", xalign=0)
        self._det_name.add_css_class("detail-name")
        self._det_name.set_ellipsize(3)

        self._det_meta = Gtk.Label(label="Select a recording to view details", xalign=0)
        self._det_meta.add_css_class("detail-meta")
        self._det_meta.set_wrap(True)

        det_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._det_play_btn = Gtk.Button()
        self._icon_btn(self._det_play_btn, "media-playback-start-symbolic", "Play")
        self._det_play_btn.add_css_class("btn-secondary")
        self._det_play_btn.connect("clicked", self._on_play_media)

        self._det_use_btn = Gtk.Button()
        self._det_use_btn.set_hexpand(True)
        self._icon_btn(self._det_use_btn, "go-next-symbolic", "Use in Media Tools")
        self._det_use_btn.add_css_class("btn-primary")
        self._det_use_btn.connect("clicked", self._on_use_selection)

        det_btns.append(self._det_play_btn)
        det_btns.append(self._det_use_btn)

        det.append(self._det_name)
        det.append(self._det_meta)
        det.append(det_btns)
        box.append(det)

        return box

    # ── Status bar ───────────────────────────────────────────

    def _build_status_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("status-bar")

        self._status_lbl = Gtk.Label(label="Ready", xalign=0)
        self._status_lbl.add_css_class("status-text")
        self._status_lbl.set_hexpand(True)

        self._timer_lbl = Gtk.Label(label="")
        self._timer_lbl.add_css_class("timer-text")

        bar.append(self._status_lbl)
        bar.append(self._timer_lbl)
        return bar

    # ── Widget helpers ───────────────────────────────────────

    def _sep(self) -> Gtk.Widget:
        s = Gtk.Box()
        s.add_css_class("sep")
        return s

    def _icon_btn(self, btn: Gtk.Button, icon: str, label: str) -> None:
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        inner.set_halign(Gtk.Align.CENTER)
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(15)
        inner.append(img)
        inner.append(Gtk.Label(label=label))
        btn.set_child(inner)

    # ── State ────────────────────────────────────────────────

    def _refresh_ui(self) -> None:
        rec = self.state.is_recording
        lib_sel = self._lib_path()

        self._folder_lbl.set_text(str(self.state.output_dir))

        self._fs_btn.set_sensitive(not rec)
        self._region_btn.set_sensitive(not rec)
        self._stop_btn.set_sensitive(rec)
        self._open_folder_btn.set_sensitive(self.recorder.last_output is not None)

        has = self.media_path is not None
        self._play_btn.set_sensitive(has)
        self._export_btn.set_sensitive(has and not rec)
        self._use_sel_btn.set_sensitive(lib_sel is not None)
        self._det_play_btn.set_sensitive(lib_sel is not None)
        self._det_use_btn.set_sensitive(lib_sel is not None)

        self._rec_chip.set_visible(rec)
        if not rec:
            self._status_lbl.remove_css_class("recording")
            self._timer_lbl.set_text("")

    def _refresh_library(self) -> None:
        while self._lib.get_row_at_index(0) is not None:
            self._lib.remove(self._lib.get_row_at_index(0))

        files = (
            sorted(
                [p for p in self.state.output_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if self.state.output_dir.exists() else []
        )

        for p in files:
            self._lib.append(FileRow(p))

        if files and self.recorder.last_output:
            for i, p in enumerate(files):
                if p == self.recorder.last_output:
                    row = self._lib.get_row_at_index(i)
                    if row:
                        self._lib.select_row(row)
                    break

    def _lib_path(self) -> Path | None:
        row = self._lib.get_selected_row()
        return row.path if row is not None else None

    def _set_media(self, path: Path) -> None:
        self.media_path = path
        self._media_name.set_text(path.name)
        self._media_meta.set_text(self.recorder.get_media_info(path))
        self._exp_status.set_text("File loaded")
        self._refresh_ui()

    # ── Capture handlers ─────────────────────────────────────

    def _on_format_changed(self, *_) -> None:
        item = self._fmt_drop.get_selected_item()
        if item:
            self.state.output_format = item.get_string()

    def _on_fps_changed(self, *_) -> None:
        item = self._fps_drop.get_selected_item()
        if item:
            self.state.fps = int(item.get_string())

    def _on_fullscreen(self, _btn: Gtk.Button) -> None:
        self.state.selected_region = None
        self._region_info.set_text("Full screen")
        self._start_recording()

    def _on_select_region(self, _btn: Gtk.Button) -> None:
        if detect_session_type() == "wayland":
            self._alert("Wayland Not Supported",
                        "Region selection is not available on Wayland yet.")
            return
        if shutil.which("slop") is None:
            self._alert("slop Required",
                        "Region selection requires 'slop'.\n\nInstall it with:\n  sudo apt install slop")
            return
        self._pick_region()

    def _pick_region(self) -> None:
        self._region_info.set_text("Drag on screen to select…")
        self.hide()

        def run() -> None:
            try:
                res = subprocess.run(
                    ["slop", "-f", "%x %y %w %h"],
                    capture_output=True, text=True, timeout=30,
                )
                if res.returncode != 0:
                    GLib.idle_add(self._region_cancelled)
                    return
                parts = res.stdout.strip().split()
                x, y, w, h = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                GLib.idle_add(self._region_done, Region(x=x, y=y, width=w, height=h))
            except (ValueError, IndexError, subprocess.SubprocessError) as exc:
                self._log.error("slop error: %s", exc)
                GLib.idle_add(self._region_cancelled)

        threading.Thread(target=run, daemon=True).start()

    def _region_done(self, region: Region) -> None:
        if not region.is_valid():
            self._region_cancelled()
            return
        self.state.selected_region = region
        self._region_info.set_text(f"{region.width}×{region.height} at ({region.x}, {region.y})")
        self._start_recording()

    def _region_cancelled(self) -> None:
        self.state.selected_region = None
        self._region_info.set_text("Selection cancelled — full screen will be used")
        self.present()
        self._refresh_ui()

    def _start_recording(self) -> None:
        try:
            output = self.recorder.start(self.state)
        except RecorderError as exc:
            self._alert("Recording Error", str(exc))
            self.present()
            self._refresh_ui()
            return

        self.state.is_recording = True
        self._rec_start = monotonic()
        self._start_timer()
        self._status_lbl.set_text("Recording…")
        self._status_lbl.add_css_class("recording")
        self._refresh_ui()
        self.hide()
        self._show_toolbar()
        self._log.info("Recording started → %s", output)

    def _on_stop(self, _btn: Gtk.Button) -> None:
        self._do_stop()

    def _do_stop(self) -> None:
        try:
            output = self.recorder.stop()
        except RecorderError as exc:
            self._alert("Stop Error", str(exc))
            return

        self.state.is_recording = False
        self._stop_timer()
        self._hide_toolbar()
        self.present()
        self._status_lbl.remove_css_class("recording")
        self._status_lbl.set_text(f"Saved: {output.name}")
        self._refresh_library()
        self._set_media(output)
        self._refresh_ui()

    def _on_choose_folder(self, _btn: Gtk.Button) -> None:
        dlg = Gtk.FileChooserNative.new(
            "Select Output Folder", self,
            Gtk.FileChooserAction.SELECT_FOLDER, "Select", "Cancel",
        )
        dlg.connect("response", self._folder_chosen)
        dlg.show()

    def _folder_chosen(self, dlg: Gtk.FileChooserNative, resp: int) -> None:
        if resp == Gtk.ResponseType.ACCEPT:
            f = dlg.get_file()
            if f and f.get_path():
                self.state.output_dir = Path(f.get_path())
                self._refresh_library()
        dlg.destroy()
        self._refresh_ui()

    def _on_show_folder(self, _btn: Gtk.Button) -> None:
        target = self.recorder.last_output or self.state.output_dir
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(target))).open_containing_folder(
            self, None, None, None
        )

    # ── Media handlers ───────────────────────────────────────

    def _on_choose_media(self, _btn: Gtk.Button) -> None:
        dlg = Gtk.FileChooserNative.new(
            "Select Media File", self,
            Gtk.FileChooserAction.OPEN, "Open", "Cancel",
        )
        dlg.connect("response", self._media_chosen)
        dlg.show()

    def _media_chosen(self, dlg: Gtk.FileChooserNative, resp: int) -> None:
        if resp == Gtk.ResponseType.ACCEPT:
            f = dlg.get_file()
            if f and f.get_path():
                self._set_media(Path(f.get_path()))
        dlg.destroy()

    def _on_use_selection(self, _btn: Gtk.Button) -> None:
        path = self._lib_path()
        if path is None:
            return
        self._set_media(path)
        self._stack.set_visible_child_name("media")

    def _on_play_media(self, _btn: Gtk.Button) -> None:
        path = self.media_path or self._lib_path()
        if path is None:
            return
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(self, None, None, None)

    def _on_export(self, _btn: Gtk.Button) -> None:
        if self.media_path is None:
            return
        fmt_item = self._exp_fmt.get_selected_item()
        spd_item = self._exp_spd.get_selected_item()
        if not fmt_item or not spd_item:
            return

        try:
            speed = float(spd_item.get_string().replace("x", ""))
        except ValueError:
            return

        fmt = fmt_item.get_string()
        path = self.media_path
        out_dir = self.state.output_dir

        self._export_btn.set_sensitive(False)
        self._exp_spinner.set_visible(True)
        self._exp_spinner.start()
        self._exp_status.set_text("Exporting…")

        def run() -> None:
            try:
                result = self.recorder.export_media(path, out_dir, fmt, speed)
                GLib.idle_add(self._export_done, result)
            except RecorderError as exc:
                GLib.idle_add(self._export_failed, str(exc))

        threading.Thread(target=run, daemon=True).start()

    def _export_done(self, path: Path) -> None:
        self._exp_spinner.stop()
        self._exp_spinner.set_visible(False)
        self._exp_status.set_text(f"Saved: {path.name}")
        self._refresh_library()
        self._set_media(path)
        self._refresh_ui()

    def _export_failed(self, msg: str) -> None:
        self._exp_spinner.stop()
        self._exp_spinner.set_visible(False)
        self._exp_status.set_text("Export failed")
        self._refresh_ui()
        self._alert("Export Error", msg)

    # ── Library handlers ─────────────────────────────────────

    def _on_row_selected(self, _lb: Gtk.ListBox, row) -> None:
        path = row.path if row else None
        if path is None:
            self._det_name.set_text("No file selected")
            self._det_meta.set_text("Select a recording to view details")
        else:
            self._det_name.set_text(path.name)
            self._det_meta.set_text(self.recorder.get_media_info(path))
        self._refresh_ui()

    # ── Toolbar ──────────────────────────────────────────────

    def _show_toolbar(self) -> None:
        if self._toolbar is None:
            self._toolbar = FloatingControlsWindow(self)
        self._toolbar.sync_toggle(False)
        self._toolbar.update(self._elapsed(), True)
        self._toolbar.present()
        self._position_toolbar()

    def _hide_toolbar(self) -> None:
        if self._toolbar:
            self._toolbar.hide()

    def stop_recording_from_toolbar(self) -> None:
        self._do_stop()

    def toggle_main_window(self) -> None:
        if self.is_visible():
            self.hide()
            if self._toolbar:
                self._toolbar.sync_toggle(False)
        else:
            self.present()
            if self._toolbar:
                self._toolbar.sync_toggle(True)

    def _position_toolbar(self) -> None:
        if detect_session_type() != "x11":
            return
        if not (shutil.which("xdotool") and shutil.which("wmctrl")):
            return
        try:
            geo = subprocess.run(
                ["xdotool", "getdisplaygeometry"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            sw = int(geo.split()[0])
            x = max(0, sw - 360)
            wid = subprocess.run(
                ["xdotool", "search", "--name", "Recorder Controls"],
                capture_output=True, text=True, check=True,
            ).stdout.strip().splitlines()[-1]
            subprocess.run(["wmctrl", "-i", "-r", wid, "-e", f"0,{x},18,-1,-1"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "add,above"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, ValueError, IndexError):
            pass

    # ── Timer ────────────────────────────────────────────────

    def _start_timer(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
        self._timer_id = GLib.timeout_add_seconds(1, self._tick)
        self._tick()

    def _stop_timer(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        self._rec_start = None
        if self._toolbar:
            self._toolbar.update("00:00", False)

    def _tick(self) -> bool:
        if not self.state.is_recording:
            return False
        err = self.recorder.poll_failure()
        if err:
            self.state.is_recording = False
            self._stop_timer()
            self._hide_toolbar()
            self.present()
            self._status_lbl.remove_css_class("recording")
            self._status_lbl.set_text("Recording failed")
            self._rec_chip.set_visible(False)
            self._alert("Recording Error", err)
            self._refresh_ui()
            return False
        e = self._elapsed()
        self._timer_lbl.set_text(e)
        if self._toolbar:
            self._toolbar.update(e, True)
        return True

    def _elapsed(self) -> str:
        if self._rec_start is None:
            return "00:00"
        s = max(0, int(monotonic() - self._rec_start))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    # ── Dialog ───────────────────────────────────────────────

    def _alert(self, title: str, body: str) -> None:
        d = Adw.MessageDialog.new(self, title, body)
        d.add_response("ok", "OK")
        d.present()


# ─────────────────────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────────────────────

class RecorderApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="com.aras.screenrecorder",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        Adw.init()

    def do_activate(self) -> None:
        win = self.props.active_window or RecorderWindow(application=self)
        win.present()
