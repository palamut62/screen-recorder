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
#  Floating Controls Window (shown during recording)
# ─────────────────────────────────────────────────────────────

class FloatingControlsWindow(Gtk.Window):
    def __init__(self, parent: "RecorderWindow") -> None:
        super().__init__(application=parent.get_application(), transient_for=parent, decorated=False)
        self.parent_window = parent
        self.set_title("Recorder Controls")
        self.set_resizable(False)
        self.set_default_size(380, 72)

        handle = Gtk.WindowHandle()
        self.set_child(handle)

        frame = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        frame.add_css_class("floating-toolbar")
        frame.set_margin_top(0)
        frame.set_margin_bottom(0)
        frame.set_margin_start(0)
        frame.set_margin_end(0)
        handle.set_child(frame)

        # Status area
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        status_box.set_hexpand(True)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._dot = Gtk.Label(label="●")
        self._dot.add_css_class("toolbar-dot")
        self._status_label = Gtk.Label(label="Recording")
        self._status_label.add_css_class("toolbar-status")
        self._status_label.set_xalign(0)
        status_row.append(self._dot)
        status_row.append(self._status_label)

        self._timer_label = Gtk.Label(label="00:00")
        self._timer_label.add_css_class("toolbar-timer")
        self._timer_label.set_xalign(0)

        status_box.append(status_row)
        status_box.append(self._timer_label)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.add_css_class("btn-danger")
        stop_btn.connect("clicked", lambda _b: self.parent_window.stop_recording_from_toolbar())

        self._toggle_btn = Gtk.Button(label="Show")
        self._toggle_btn.add_css_class("btn-secondary")
        self._toggle_btn.connect("clicked", lambda _b: self.parent_window.toggle_main_window())

        frame.append(status_box)
        frame.append(stop_btn)
        frame.append(self._toggle_btn)

    def sync_toggle_label(self, is_visible: bool) -> None:
        self._toggle_btn.set_label("Hide" if is_visible else "Show")

    def update_info(self, elapsed: str, is_recording: bool) -> None:
        self._status_label.set_text("Recording" if is_recording else "Stopped")
        self._timer_label.set_text(elapsed)


# ─────────────────────────────────────────────────────────────
#  Library file row
# ─────────────────────────────────────────────────────────────

class RecordedFileRow(Gtk.ListBoxRow):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_margin_top(3)
        outer.set_margin_bottom(3)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row_box.add_css_class("lib-row")
        row_box.set_margin_top(8)
        row_box.set_margin_bottom(8)
        row_box.set_margin_start(10)
        row_box.set_margin_end(10)

        # File type icon
        icon_name = "video-x-generic-symbolic"
        if path.suffix.lower() == ".gif":
            icon_name = "image-x-generic-symbolic"
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(20)
        icon.set_opacity(0.6)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        name_label = Gtk.Label(label=path.name, xalign=0)
        name_label.add_css_class("lib-filename")
        name_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END

        ext_label = Gtk.Label(label=path.suffix.lstrip(".").upper(), xalign=0)
        ext_label.add_css_class("lib-meta")

        text_box.append(name_label)
        text_box.append(ext_label)

        row_box.append(icon)
        row_box.append(text_box)
        outer.append(row_box)
        self.set_child(outer)


# ─────────────────────────────────────────────────────────────
#  Main application window
# ─────────────────────────────────────────────────────────────

class RecorderWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._logger = logging.getLogger(__name__)
        self.state = AppState()
        self.recorder = FFmpegRecorder()
        self._toolbar_window: FloatingControlsWindow | None = None
        self._rec_started_at: float | None = None
        self._rec_timer_id: int | None = None
        self._toolbar_timeout_id: int | None = None
        self.selected_media_path: Path | None = None

        self.set_title("Screen Recorder")
        self.set_default_size(980, 580)
        self._load_css()

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self._build_content())
        self.set_content(toolbar_view)

        self._refresh_library()
        self._refresh_ui()
        self._logger.info("Main window initialized")

    # ── CSS ─────────────────────────────────────────────────

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(_CSS_PATH))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # ── Layout builders ──────────────────────────────────────

    def _build_content(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        root.add_css_class("window-shell")
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(14)
        root.set_margin_end(14)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.set_size_request(400, -1)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right.set_hexpand(True)

        left.append(self._build_brand())
        left.append(self._build_tabs())

        right.append(self._build_library_panel())

        root.append(left)
        root.append(right)
        return root

    def _build_brand(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.add_css_class("hero-card")

        icon = Gtk.Image.new_from_icon_name("media-record-symbolic")
        icon.set_pixel_size(40)
        icon.add_css_class("brand-logo")

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        text.set_hexpand(True)

        badge_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        badge = Gtk.Label(label="SCREEN RECORDER", xalign=0)
        badge.add_css_class("hero-badge")
        badge_row.append(badge)

        self._session_badge = Gtk.Label(label=detect_session_type().upper())
        self._session_badge.add_css_class("session-badge")
        badge_row.set_hexpand(True)

        title = Gtk.Label(label="Screen Recorder", xalign=0)
        title.add_css_class("brand-title")

        subtitle = Gtk.Label(label="Capture, convert and export in one place", xalign=0)
        subtitle.add_css_class("brand-subtitle")

        text.append(badge_row)
        text.append(title)
        text.append(subtitle)

        card.append(icon)
        card.append(text)
        return card

    def _build_tabs(self) -> Gtk.Widget:
        self._stack = Adw.ViewStack()
        self._stack.set_vexpand(True)
        self._stack.add_titled(self._build_capture_page(), "capture", "Capture")
        self._stack.add_titled(self._build_media_page(), "media", "Media Tools")

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(switcher)
        box.append(self._stack)
        return box

    # ── Capture page ─────────────────────────────────────────

    def _build_capture_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.add_css_class("card-elevated")
        page.set_margin_top(2)

        # Header
        page.append(self._section_header(
            "Capture",
            "media-record-symbolic",
            "Record screen or region",
        ))
        page.append(self._build_separator())

        # Mode row
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mode_box.set_margin_start(14)
        mode_box.set_margin_end(14)

        self._fullscreen_btn = Gtk.Button()
        self._fullscreen_btn.set_hexpand(True)
        self._build_icon_label_button(self._fullscreen_btn, "view-fullscreen-symbolic", "Full Screen")
        self._fullscreen_btn.add_css_class("btn-primary")
        self._fullscreen_btn.connect("clicked", self._on_fullscreen_clicked)

        self._region_btn = Gtk.Button()
        self._region_btn.set_hexpand(True)
        self._build_icon_label_button(self._region_btn, "object-select-symbolic", "Select Region")
        self._region_btn.add_css_class("btn-secondary")
        self._region_btn.connect("clicked", self._on_select_region_clicked)

        mode_box.append(self._fullscreen_btn)
        mode_box.append(self._region_btn)
        page.append(mode_box)

        # Region info pill
        self._region_pill = Gtk.Label(label="No region selected — full screen will be used", xalign=0)
        self._region_pill.add_css_class("info-pill")
        self._region_pill.set_margin_start(14)
        self._region_pill.set_margin_end(14)
        page.append(self._region_pill)

        # Format and FPS row
        prefs_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prefs_box.set_margin_start(14)
        prefs_box.set_margin_end(14)
        prefs_box.append(self._build_dropdown("Format", ["mp4", "mkv", "webm"], "_fmt_drop", self._on_format_changed))
        prefs_box.append(self._build_dropdown("FPS", ["30", "60"], "_fps_drop", self._on_fps_changed))
        page.append(prefs_box)

        # Output folder
        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_box.add_css_class("pref-row")
        folder_box.set_margin_start(14)
        folder_box.set_margin_end(14)

        folder_icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        folder_icon.set_opacity(0.6)

        self._folder_label = Gtk.Label(xalign=0)
        self._folder_label.add_css_class("pref-label")
        self._folder_label.set_hexpand(True)
        self._folder_label.set_ellipsize(3)

        choose_folder_btn = Gtk.Button(label="Change")
        choose_folder_btn.add_css_class("btn-secondary")
        choose_folder_btn.connect("clicked", self._on_choose_folder_clicked)

        folder_box.append(folder_icon)
        folder_box.append(self._folder_label)
        folder_box.append(choose_folder_btn)
        page.append(folder_box)

        page.append(self._build_separator())

        # Stop / Show folder controls
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_box.set_margin_start(14)
        action_box.set_margin_end(14)

        self._stop_btn = Gtk.Button()
        self._stop_btn.set_hexpand(True)
        self._build_icon_label_button(self._stop_btn, "media-playback-stop-symbolic", "Stop Recording")
        self._stop_btn.add_css_class("btn-danger")
        self._stop_btn.connect("clicked", self._on_stop_clicked)

        self._show_folder_btn = Gtk.Button()
        self._build_icon_label_button(self._show_folder_btn, "folder-open-symbolic", "Show Folder")
        self._show_folder_btn.add_css_class("btn-secondary")
        self._show_folder_btn.connect("clicked", self._on_show_folder_clicked)

        action_box.append(self._stop_btn)
        action_box.append(self._show_folder_btn)
        page.append(action_box)

        # Status bar
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_box.set_margin_start(14)
        status_box.set_margin_end(14)
        status_box.set_margin_bottom(14)

        self._rec_dot = Gtk.Label(label="●")
        self._rec_dot.add_css_class("rec-dot")
        self._rec_dot.set_opacity(0)

        self._rec_label = Gtk.Label(label="Ready to capture", xalign=0)
        self._rec_label.add_css_class("rec-label")
        self._rec_label.set_hexpand(True)

        self._rec_timer = Gtk.Label(label="")
        self._rec_timer.add_css_class("rec-timer")

        status_box.append(self._rec_dot)
        status_box.append(self._rec_label)
        status_box.append(self._rec_timer)
        page.append(status_box)

        return page

    # ── Media Tools page ─────────────────────────────────────

    def _build_media_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.add_css_class("card-elevated")
        page.set_margin_top(2)

        page.append(self._section_header(
            "Media Tools",
            "emblem-photos-symbolic",
            "Convert format or adjust speed",
        ))
        page.append(self._build_separator())

        # Source selection
        src_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        src_box.set_margin_start(14)
        src_box.set_margin_end(14)

        choose_btn = Gtk.Button()
        choose_btn.set_hexpand(True)
        self._build_icon_label_button(choose_btn, "document-open-symbolic", "Open File")
        choose_btn.add_css_class("btn-secondary")
        choose_btn.connect("clicked", self._on_choose_media_clicked)

        self._use_selected_btn = Gtk.Button()
        self._use_selected_btn.set_hexpand(True)
        self._build_icon_label_button(self._use_selected_btn, "go-next-symbolic", "Use Selection")
        self._use_selected_btn.add_css_class("btn-secondary")
        self._use_selected_btn.connect("clicked", self._on_use_library_selection_clicked)

        src_box.append(choose_btn)
        src_box.append(self._use_selected_btn)
        page.append(src_box)

        # Media info card
        info_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info_card.add_css_class("pref-row")
        info_card.set_margin_start(14)
        info_card.set_margin_end(14)

        self._media_file_label = Gtk.Label(label="No media selected", xalign=0)
        self._media_file_label.add_css_class("detail-title")
        self._media_file_label.set_ellipsize(3)

        self._media_meta_label = Gtk.Label(
            label="Select a file from the library or open an external file", xalign=0
        )
        self._media_meta_label.add_css_class("detail-meta")
        self._media_meta_label.set_wrap(True)

        info_card.append(self._media_file_label)
        info_card.append(self._media_meta_label)
        page.append(info_card)

        # Export options
        opts_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        opts_box.set_margin_start(14)
        opts_box.set_margin_end(14)
        opts_box.append(self._build_dropdown("Export As", ["mp4", "mkv", "webm", "gif"], "_export_fmt_drop", None))
        opts_box.append(self._build_dropdown("Speed", ["0.5x", "1.0x", "1.5x", "2.0x", "4.0x"], "_speed_drop", None))
        getattr(self, "_speed_drop").set_selected(1)
        page.append(opts_box)

        page.append(self._build_separator())

        # Export / Open actions
        exp_action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        exp_action_box.set_margin_start(14)
        exp_action_box.set_margin_end(14)

        self._open_media_btn = Gtk.Button()
        self._build_icon_label_button(self._open_media_btn, "media-playback-start-symbolic", "Play")
        self._open_media_btn.add_css_class("btn-secondary")
        self._open_media_btn.connect("clicked", self._on_open_media_clicked)

        self._export_btn = Gtk.Button()
        self._export_btn.set_hexpand(True)
        self._build_icon_label_button(self._export_btn, "document-save-symbolic", "Export")
        self._export_btn.add_css_class("btn-primary")
        self._export_btn.connect("clicked", self._on_export_media_clicked)

        exp_action_box.append(self._open_media_btn)
        exp_action_box.append(self._export_btn)
        page.append(exp_action_box)

        # Export spinner + status
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_box.set_margin_start(14)
        status_box.set_margin_end(14)
        status_box.set_margin_bottom(14)

        self._export_spinner = Gtk.Spinner()
        self._export_spinner.set_visible(False)

        self._media_status_label = Gtk.Label(label="Ready", xalign=0)
        self._media_status_label.add_css_class("rec-label")
        self._media_status_label.set_hexpand(True)

        status_box.append(self._export_spinner)
        status_box.append(self._media_status_label)
        page.append(status_box)

        return page

    # ── Library panel ─────────────────────────────────────────

    def _build_library_panel(self) -> Gtk.Widget:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        panel.add_css_class("card-elevated")
        panel.set_hexpand(True)
        panel.set_vexpand(True)
        panel.set_margin_start(2)

        # Header with refresh
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_margin_start(14)
        header_box.set_margin_end(14)
        hdr = self._section_header("Recordings", "folder-videos-symbolic", "Files in output folder")
        hdr.set_hexpand(True)

        refresh_btn = Gtk.Button()
        self._build_icon_label_button(refresh_btn, "view-refresh-symbolic", "Refresh")
        refresh_btn.add_css_class("btn-secondary")
        refresh_btn.connect("clicked", self._on_refresh_library_clicked)

        header_box.append(hdr)
        header_box.append(refresh_btn)
        panel.append(header_box)
        panel.append(self._build_separator())

        # Scrollable list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._library_list = Gtk.ListBox()
        self._library_list.add_css_class("library-list")
        self._library_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._library_list.connect("row-selected", self._on_library_row_selected)

        scroll.set_child(self._library_list)
        panel.append(scroll)

        panel.append(self._build_separator())

        # Detail card
        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        detail.add_css_class("detail-card")
        detail.set_margin_start(14)
        detail.set_margin_end(14)
        detail.set_margin_bottom(14)

        self._lib_selected_label = Gtk.Label(label="No recording selected", xalign=0)
        self._lib_selected_label.add_css_class("detail-title")
        self._lib_selected_label.set_ellipsize(3)

        self._lib_detail_label = Gtk.Label(
            label="Pick a file from the list above to view info and use it in Media Tools", xalign=0
        )
        self._lib_detail_label.add_css_class("detail-meta")
        self._lib_detail_label.set_wrap(True)

        lib_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._lib_open_btn = Gtk.Button()
        self._build_icon_label_button(self._lib_open_btn, "media-playback-start-symbolic", "Play")
        self._lib_open_btn.add_css_class("btn-secondary")
        self._lib_open_btn.connect("clicked", self._on_open_media_clicked)

        self._lib_use_btn = Gtk.Button()
        self._lib_use_btn.set_hexpand(True)
        self._build_icon_label_button(self._lib_use_btn, "go-next-symbolic", "Use in Media Tools")
        self._lib_use_btn.add_css_class("btn-primary")
        self._lib_use_btn.connect("clicked", self._on_use_library_selection_clicked)

        lib_actions.append(self._lib_open_btn)
        lib_actions.append(self._lib_use_btn)

        detail.append(self._lib_selected_label)
        detail.append(self._lib_detail_label)
        detail.append(lib_actions)
        panel.append(detail)

        return panel

    # ── Shared widget helpers ─────────────────────────────────

    def _section_header(self, title: str, icon_name: str, subtitle: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_margin_top(12)

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(18)
        icon.set_opacity(0.65)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_lbl = Gtk.Label(label=title, xalign=0)
        title_lbl.add_css_class("section-title")
        sub_lbl = Gtk.Label(label=subtitle, xalign=0)
        sub_lbl.add_css_class("section-subtitle")
        text_box.append(title_lbl)
        text_box.append(sub_lbl)

        box.append(icon)
        box.append(text_box)
        return box

    def _build_separator(self) -> Gtk.Widget:
        sep = Gtk.Box()
        sep.add_css_class("divider")
        sep.set_margin_start(14)
        sep.set_margin_end(14)
        return sep

    def _build_icon_label_button(self, button: Gtk.Button, icon_name: str, label: str) -> None:
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        inner.set_halign(Gtk.Align.CENTER)
        img = Gtk.Image.new_from_icon_name(icon_name)
        img.set_pixel_size(16)
        lbl = Gtk.Label(label=label)
        inner.append(img)
        inner.append(lbl)
        button.set_child(inner)

    def _build_dropdown(
        self,
        label_text: str,
        items: list[str],
        attr_name: str,
        callback,
    ) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("pref-row")
        box.set_hexpand(True)

        lbl = Gtk.Label(label=label_text)
        lbl.add_css_class("pref-label")

        model = Gtk.StringList.new(items)
        drop = Gtk.DropDown(model=model)
        drop.set_hexpand(True)

        if callback is not None:
            drop.connect("notify::selected-item", callback)

        setattr(self, attr_name, drop)

        box.append(lbl)
        box.append(drop)
        return box

    # ── State refresh ─────────────────────────────────────────

    def _refresh_ui(self) -> None:
        recording = self.state.is_recording

        self._folder_label.set_text(str(self.state.output_dir))

        self._fullscreen_btn.set_sensitive(not recording)
        self._region_btn.set_sensitive(not recording)
        self._stop_btn.set_sensitive(recording)
        self._show_folder_btn.set_sensitive(self.recorder.last_output is not None)

        has_media = self.selected_media_path is not None
        self._open_media_btn.set_sensitive(has_media)
        self._export_btn.set_sensitive(has_media and not recording)
        self._use_selected_btn.set_sensitive(self._selected_library_path() is not None)
        self._lib_open_btn.set_sensitive(self._selected_library_path() is not None)
        self._lib_use_btn.set_sensitive(self._selected_library_path() is not None)

        self._rec_dot.set_opacity(1.0 if recording else 0.0)
        if not recording:
            self._rec_label.set_text("Ready to capture")
            self._rec_timer.set_text("")

    def _refresh_library(self) -> None:
        while True:
            row = self._library_list.get_row_at_index(0)
            if row is None:
                break
            self._library_list.remove(row)

        files = (
            sorted(
                [p for p in self.state.output_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if self.state.output_dir.exists()
            else []
        )

        for path in files:
            self._library_list.append(RecordedFileRow(path))

        if files and self.recorder.last_output is not None:
            for idx, path in enumerate(files):
                if path == self.recorder.last_output:
                    row = self._library_list.get_row_at_index(idx)
                    if row is not None:
                        self._library_list.select_row(row)
                    break

    def _selected_library_path(self) -> Path | None:
        row = self._library_list.get_selected_row()
        return row.path if row is not None else None

    def _sync_media_selection(self, path: Path) -> None:
        self.selected_media_path = path
        self._media_file_label.set_text(path.name)
        self._media_meta_label.set_text(self.recorder.get_media_info(path))
        self._media_status_label.set_text("Media loaded")
        self._refresh_ui()

    # ── Signal handlers: capture ──────────────────────────────

    def _on_format_changed(self, *_args) -> None:
        item = self._fmt_drop.get_selected_item()
        if item is not None:
            self.state.output_format = item.get_string()

    def _on_fps_changed(self, *_args) -> None:
        item = self._fps_drop.get_selected_item()
        if item is not None:
            self.state.fps = int(item.get_string())

    def _on_fullscreen_clicked(self, _btn: Gtk.Button) -> None:
        self.state.selected_region = None
        self._region_pill.set_text("No region selected — full screen will be used")
        self._begin_recording()

    def _on_select_region_clicked(self, _btn: Gtk.Button) -> None:
        if detect_session_type() == "wayland":
            self._show_message("Wayland Not Supported", "Region selection is not yet available on Wayland.")
            return

        if shutil.which("slop") is not None:
            self._pick_region_with_slop()
        else:
            self._show_message(
                "slop Gerekli",
                "Bölge seçimi için 'slop' aracı gereklidir.\n\nKurmak için:\n  sudo apt install slop",
            )

    def _pick_region_with_slop(self) -> None:
        self._region_pill.set_text("Drag on screen to select a region…")
        self.hide()

        def run_slop():
            try:
                result = subprocess.run(
                    ["slop", "-f", "%x %y %w %h"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    GLib.idle_add(self._on_slop_cancelled)
                    return
                parts = result.stdout.strip().split()
                x, y, w, h = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                region = Region(x=x, y=y, width=w, height=h)
                GLib.idle_add(self._on_slop_done, region)
            except Exception as exc:
                self._logger.exception("slop failed: %s", exc)
                GLib.idle_add(self._on_slop_cancelled)

        threading.Thread(target=run_slop, daemon=True).start()

    def _on_slop_done(self, region: Region) -> None:
        if not region.is_valid():
            self._on_slop_cancelled()
            return
        self.state.selected_region = region
        self._region_pill.set_text(f"Bölge: {region.width}×{region.height} — ({region.x}, {region.y})")
        self._begin_recording()

    def _on_slop_cancelled(self) -> None:
        self.state.selected_region = None
        self._region_pill.set_text("Region selection cancelled")
        self.present()
        self._refresh_ui()

    def _begin_recording(self) -> None:
        self._logger.info("Begin recording | region=%s", self.state.selected_region)
        try:
            output_path = self.recorder.start(self.state)
        except RecorderError as exc:
            self._logger.exception("Recording failed to start: %s", exc)
            self._show_message("Recording Error", str(exc))
            self.present()
            self._refresh_ui()
            return

        self.state.is_recording = True
        self._rec_started_at = monotonic()
        self._start_rec_timer()
        self._rec_label.set_text("Recording…")
        self._rec_dot.set_opacity(1.0)
        self._refresh_ui()

        self.hide()
        self._present_toolbar()

        self._logger.info("Recording started | output=%s", output_path)

    def _on_stop_clicked(self, _btn: Gtk.Button) -> None:
        self._stop_recording()

    def _stop_recording(self) -> None:
        self._logger.info("Stop recording")
        try:
            output_path = self.recorder.stop()
        except RecorderError as exc:
            self._logger.exception("Stop failed: %s", exc)
            self._show_message("Stop Error", str(exc))
            return

        self.state.is_recording = False
        self._stop_rec_timer()
        self._cancel_toolbar_timeout()
        self._hide_toolbar()
        self.present()
        self._rec_label.set_text(f"Saved: {output_path.name}")
        self._refresh_library()
        self._sync_media_selection(output_path)
        self._refresh_ui()

    def _on_choose_folder_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileChooserNative.new(
            "Select Output Folder", self,
            Gtk.FileChooserAction.SELECT_FOLDER, "Select", "Cancel",
        )
        dialog.connect("response", self._on_folder_chosen)
        dialog.show()

    def _on_folder_chosen(self, dialog: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            selected = dialog.get_file()
            if selected is not None and selected.get_path() is not None:
                self.state.output_dir = Path(selected.get_path())
                self._refresh_library()
        dialog.destroy()
        self._refresh_ui()

    def _on_show_folder_clicked(self, _btn: Gtk.Button) -> None:
        target = self.recorder.last_output or self.state.output_dir
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(target)))
        launcher.open_containing_folder(self, None, None, None)

    # ── Signal handlers: media ────────────────────────────────

    def _on_choose_media_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileChooserNative.new(
            "Select Media File", self,
            Gtk.FileChooserAction.OPEN, "Open", "Cancel",
        )
        dialog.connect("response", self._on_media_chosen)
        dialog.show()

    def _on_media_chosen(self, dialog: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            selected = dialog.get_file()
            if selected is not None and selected.get_path() is not None:
                self._sync_media_selection(Path(selected.get_path()))
        dialog.destroy()

    def _on_use_library_selection_clicked(self, _btn: Gtk.Button) -> None:
        path = self._selected_library_path()
        if path is None:
            return
        self._sync_media_selection(path)
        self._stack.set_visible_child_name("media")

    def _on_open_media_clicked(self, _btn: Gtk.Button) -> None:
        path = self.selected_media_path or self._selected_library_path()
        if path is None:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
        launcher.launch(self, None, None, None)

    def _on_export_media_clicked(self, _btn: Gtk.Button) -> None:
        if self.selected_media_path is None:
            return
        fmt_item = self._export_fmt_drop.get_selected_item()
        spd_item = self._speed_drop.get_selected_item()
        if fmt_item is None or spd_item is None:
            return

        output_format = fmt_item.get_string()
        speed_factor = float(spd_item.get_string().replace("x", ""))
        input_path = self.selected_media_path
        output_dir = self.state.output_dir

        self._export_btn.set_sensitive(False)
        self._export_spinner.set_visible(True)
        self._export_spinner.start()
        self._media_status_label.set_text("Exporting…")

        def do_export():
            try:
                result = self.recorder.export_media(
                    input_path=input_path,
                    output_dir=output_dir,
                    output_format=output_format,
                    speed_factor=speed_factor,
                )
                GLib.idle_add(self._on_export_done, result)
            except RecorderError as exc:
                GLib.idle_add(self._on_export_failed, str(exc))

        threading.Thread(target=do_export, daemon=True).start()

    def _on_export_done(self, output_path: Path) -> None:
        self._export_spinner.stop()
        self._export_spinner.set_visible(False)
        self._media_status_label.set_text(f"Saved: {output_path.name}")
        self._refresh_library()
        self._sync_media_selection(output_path)
        self._refresh_ui()

    def _on_export_failed(self, error_text: str) -> None:
        self._export_spinner.stop()
        self._export_spinner.set_visible(False)
        self._media_status_label.set_text("Export failed")
        self._refresh_ui()
        self._show_message("Export Error", error_text)

    # ── Library events ────────────────────────────────────────

    def _on_library_row_selected(self, _listbox: Gtk.ListBox, row) -> None:
        path = row.path if row is not None else None
        if path is None:
            self._lib_selected_label.set_text("No recording selected")
            self._lib_detail_label.set_text(
                "Pick a file from the list above to view info and use it in Media Tools"
            )
        else:
            self._lib_selected_label.set_text(path.name)
            self._lib_detail_label.set_text(self.recorder.get_media_info(path))
        self._refresh_ui()

    def _on_refresh_library_clicked(self, _btn: Gtk.Button) -> None:
        self._refresh_library()
        self._refresh_ui()

    # ── Toolbar management ────────────────────────────────────

    def _present_toolbar(self) -> None:
        if self._toolbar_window is None:
            self._toolbar_window = FloatingControlsWindow(self)
        self._toolbar_window.sync_toggle_label(False)
        self._toolbar_window.update_info(self._format_elapsed(), True)
        self._toolbar_window.present()
        self._move_toolbar_top_right()

    def _hide_toolbar(self) -> None:
        if self._toolbar_window is not None:
            self._toolbar_window.hide()

    def stop_recording_from_toolbar(self) -> None:
        self._stop_recording()

    def toggle_main_window(self) -> None:
        if self.is_visible():
            self.hide()
            if self._toolbar_window:
                self._toolbar_window.sync_toggle_label(False)
        else:
            self.present()
            if self._toolbar_window:
                self._toolbar_window.sync_toggle_label(True)

    def quit_from_toolbar(self) -> None:
        if self.state.is_recording:
            self._stop_recording()
        app = self.get_application()
        if app:
            app.quit()

    def _toolbar_after_selection(self) -> bool:
        self._toolbar_timeout_id = None
        if self.state.is_recording:
            self._present_toolbar()
        return False

    def _cancel_toolbar_timeout(self) -> None:
        if self._toolbar_timeout_id is not None:
            GLib.source_remove(self._toolbar_timeout_id)
            self._toolbar_timeout_id = None

    def _move_toolbar_top_right(self) -> None:
        if detect_session_type() != "x11":
            return
        if shutil.which("xdotool") is None or shutil.which("wmctrl") is None:
            return
        try:
            geometry = subprocess.run(
                ["xdotool", "getdisplaygeometry"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            screen_width = int(geometry.split()[0])
            x = max(0, screen_width - 400)
            y = 18

            win_id = subprocess.run(
                ["xdotool", "search", "--name", "Recorder Controls"],
                check=True, capture_output=True, text=True,
            ).stdout.strip().splitlines()[-1]

            subprocess.run(
                ["wmctrl", "-i", "-r", win_id, "-e", f"0,{x},{y},-1,-1"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["wmctrl", "-i", "-r", win_id, "-b", "add,above"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (subprocess.SubprocessError, ValueError, IndexError):
            pass

    # ── Recording timer ───────────────────────────────────────

    def _start_rec_timer(self) -> None:
        if self._rec_timer_id is not None:
            GLib.source_remove(self._rec_timer_id)
        self._rec_timer_id = GLib.timeout_add_seconds(1, self._tick_rec_timer)
        self._tick_rec_timer()

    def _stop_rec_timer(self) -> None:
        if self._rec_timer_id is not None:
            GLib.source_remove(self._rec_timer_id)
            self._rec_timer_id = None
        self._rec_started_at = None
        if self._toolbar_window:
            self._toolbar_window.update_info("00:00", False)

    def _tick_rec_timer(self) -> bool:
        if not self.state.is_recording:
            return False
        error = self.recorder.poll_failure()
        if error is not None:
            self._logger.error("Process failure detected: %s", error)
            self.state.is_recording = False
            self._stop_rec_timer()
            self._cancel_toolbar_timeout()
            self._hide_toolbar()
            self.present()
            self._rec_label.set_text("Recording failed")
            self._rec_dot.set_opacity(0)
            self._show_message("Recording Error", error)
            self._refresh_ui()
            return False
        elapsed = self._format_elapsed()
        self._rec_timer.set_text(elapsed)
        if self._toolbar_window:
            self._toolbar_window.update_info(elapsed, True)
        return True

    def _format_elapsed(self) -> str:
        if self._rec_started_at is None:
            return "00:00"
        secs = max(0, int(monotonic() - self._rec_started_at))
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    # ── Dialogs ───────────────────────────────────────────────

    def _show_message(self, heading: str, body: str) -> None:
        dialog = Adw.MessageDialog.new(self, heading, body)
        dialog.add_response("ok", "OK")
        dialog.present()


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
        window = self.props.active_window
        if window is None:
            window = RecorderWindow(application=self)
        window.present()
