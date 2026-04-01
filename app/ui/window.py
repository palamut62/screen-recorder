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

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk

from app.core.state import AppState, Region
from app.recorder.ffmpeg import FFmpegRecorder, RecorderError
from app.utils.env import detect_session_type

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".gif", ".mov", ".avi"}
_CSS_PATH   = Path(__file__).parent / "style.css"
_ROBOT_PATH = Path(__file__).parent.parent.parent / "assets" / "icons" / "robot.png"


# ─────────────────────────────────────────────────────────────
#  Floating toolbar
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
        self.set_default_size(320, 60)

        handle = Gtk.WindowHandle()
        self.set_child(handle)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.add_css_class("floating-toolbar")
        handle.set_child(bar)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        info.set_hexpand(True)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self._dot = Gtk.Label(label="●")
        self._dot.add_css_class("toolbar-dot")
        self._status = Gtk.Label(label="Recording")
        self._status.add_css_class("toolbar-status")
        self._status.set_xalign(0)
        top.append(self._dot)
        top.append(self._status)

        self._timer = Gtk.Label(label="00:00")
        self._timer.add_css_class("toolbar-timer")
        self._timer.set_xalign(0)

        info.append(top)
        info.append(self._timer)

        stop_btn = Gtk.Button(label="■  Stop")
        stop_btn.add_css_class("btn-stop")
        stop_btn.connect("clicked", lambda _: self.parent_window.stop_recording_from_toolbar())

        self._toggle = Gtk.Button(label="Show")
        self._toggle.add_css_class("btn-secondary")
        self._toggle.connect("clicked", lambda _: self.parent_window.toggle_main_window())

        bar.append(info)
        bar.append(stop_btn)
        bar.append(self._toggle)

    def sync_toggle(self, visible: bool) -> None:
        self._toggle.set_label("Hide" if visible else "Show")

    def update(self, elapsed: str, recording: bool) -> None:
        self._status.set_text("Recording" if recording else "Stopped")
        self._timer.set_text(elapsed)


# ─────────────────────────────────────────────────────────────
#  Library file row
# ─────────────────────────────────────────────────────────────

class FileRow(Gtk.ListBoxRow):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("lib-row")
        box.set_margin_top(1)
        box.set_margin_bottom(1)
        box.set_margin_start(2)
        box.set_margin_end(2)

        icon_name = "image-x-generic-symbolic" if path.suffix.lower() == ".gif" \
                    else "video-x-generic-symbolic"
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(15)
        icon.set_opacity(0.50)

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
#  Recordings popover
# ─────────────────────────────────────────────────────────────

class RecordingsPopover(Gtk.Popover):
    """Popover that shows the recordings library and detail/actions."""

    def __init__(self, parent: "RecorderWindow") -> None:
        super().__init__()
        self.pw = parent
        self.set_size_request(380, 460)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Header
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hdr.set_margin_top(12)
        hdr.set_margin_bottom(8)
        hdr.set_margin_start(14)
        hdr.set_margin_end(10)

        title = Gtk.Label(label="Recordings", xalign=0)
        title.add_css_class("pop-title")
        title.set_hexpand(True)

        ref = Gtk.Button()
        img = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        img.set_pixel_size(14)
        ref.set_child(img)
        ref.add_css_class("btn-icon-pill")
        ref.connect("clicked", lambda _: self._refresh())

        hdr.append(title)
        hdr.append(ref)
        root.append(hdr)

        sep1 = Gtk.Box()
        sep1.add_css_class("sep")
        root.append(sep1)

        # List
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._lib = Gtk.ListBox()
        self._lib.add_css_class("lib-list")
        self._lib.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._lib.connect("row-selected", self._on_selected)
        scroll.set_child(self._lib)
        root.append(scroll)

        sep2 = Gtk.Box()
        sep2.add_css_class("sep")
        root.append(sep2)

        # Detail strip
        det = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        det.set_margin_top(10)
        det.set_margin_bottom(10)
        det.set_margin_start(12)
        det.set_margin_end(12)

        self._det_name = Gtk.Label(label="No file selected", xalign=0)
        self._det_name.add_css_class("lib-name")
        self._det_name.set_ellipsize(3)

        self._det_meta = Gtk.Label(label="Select a recording", xalign=0)
        self._det_meta.add_css_class("lib-meta")
        self._det_meta.set_wrap(True)

        det_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._play_btn = Gtk.Button()
        self._il_btn(self._play_btn, "media-playback-start-symbolic", "Play")
        self._play_btn.add_css_class("btn-secondary")
        self._play_btn.connect("clicked", self._on_play)

        self._use_btn = Gtk.Button()
        self._use_btn.set_hexpand(True)
        self._il_btn(self._use_btn, "go-next-symbolic", "Use in Media Tools")
        self._use_btn.add_css_class("btn-primary")
        self._use_btn.connect("clicked", self._on_use)

        det_btns.append(self._play_btn)
        det_btns.append(self._use_btn)

        det.append(self._det_name)
        det.append(self._det_meta)
        det.append(det_btns)
        root.append(det)

        self.set_child(root)
        self._refresh()

    def _il_btn(self, btn: Gtk.Button, icon: str, label: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        box.set_halign(Gtk.Align.CENTER)
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(14)
        box.append(img)
        box.append(Gtk.Label(label=label))
        btn.set_child(box)

    def refresh(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        while self._lib.get_row_at_index(0):
            self._lib.remove(self._lib.get_row_at_index(0))

        files = (
            sorted(
                [p for p in self.pw.state.output_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS],
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            if self.pw.state.output_dir.exists() else []
        )

        for p in files:
            self._lib.append(FileRow(p))

        if files and self.pw.recorder.last_output:
            for i, p in enumerate(files):
                if p == self.pw.recorder.last_output:
                    row = self._lib.get_row_at_index(i)
                    if row:
                        self._lib.select_row(row)
                    break

        no_sel = self._lib_path() is None
        self._play_btn.set_sensitive(not no_sel)
        self._use_btn.set_sensitive(not no_sel)

    def _lib_path(self) -> Path | None:
        row = self._lib.get_selected_row()
        return row.path if row else None

    def _on_selected(self, _lb, row) -> None:
        path = row.path if row else None
        if path is None:
            self._det_name.set_text("No file selected")
            self._det_meta.set_text("Select a recording")
        else:
            self._det_name.set_text(path.name)
            self._det_meta.set_text(self.pw.recorder.get_media_info(path))
        no_sel = path is None
        self._play_btn.set_sensitive(not no_sel)
        self._use_btn.set_sensitive(not no_sel)

    def _on_play(self, _btn) -> None:
        p = self._lib_path()
        if p:
            Gtk.FileLauncher.new(Gio.File.new_for_path(str(p))).launch(
                self.pw, None, None, None)

    def _on_use(self, _btn) -> None:
        p = self._lib_path()
        if p:
            self.pw.set_media(p)
            self.popdown()
            self.pw.open_media_tools()


# ─────────────────────────────────────────────────────────────
#  Media Tools popover
# ─────────────────────────────────────────────────────────────

class MediaToolsPopover(Gtk.Popover):
    def __init__(self, parent: "RecorderWindow") -> None:
        super().__init__()
        self.pw = parent
        self.set_size_request(360, -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(14)
        root.set_margin_end(14)

        title = Gtk.Label(label="Media Tools", xalign=0)
        title.add_css_class("pop-title")
        root.append(title)

        # Source buttons
        src = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        open_btn = Gtk.Button()
        open_btn.set_hexpand(True)
        self._il(open_btn, "document-open-symbolic", "Open File")
        open_btn.add_css_class("btn-secondary")
        open_btn.connect("clicked", self._on_open)
        src.append(open_btn)
        root.append(src)

        # File info
        self._file_lbl = Gtk.Label(label="No file loaded", xalign=0)
        self._file_lbl.add_css_class("lib-name")
        self._file_lbl.set_ellipsize(3)
        self._meta_lbl = Gtk.Label(label="Open a file or use a library selection", xalign=0)
        self._meta_lbl.add_css_class("lib-meta")
        self._meta_lbl.set_wrap(True)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        info_box.add_css_class("row-box")
        info_box.append(self._file_lbl)
        info_box.append(self._meta_lbl)
        root.append(info_box)

        # Options
        opts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)

        fmt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fmt_row.add_css_class("row-box")
        fl = Gtk.Label(label="Export As", xalign=0)
        fl.add_css_class("row-label")
        fl.set_hexpand(True)
        self._fmt = Gtk.DropDown(model=Gtk.StringList.new(["mp4", "mkv", "webm", "gif"]))
        fmt_row.append(fl)
        fmt_row.append(self._fmt)

        spd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spd_row.add_css_class("row-box")
        sl = Gtk.Label(label="Speed", xalign=0)
        sl.add_css_class("row-label")
        sl.set_hexpand(True)
        self._spd = Gtk.DropDown(model=Gtk.StringList.new(["0.5x", "1.0x", "1.5x", "2.0x", "4.0x"]))
        self._spd.set_selected(1)
        spd_row.append(sl)
        spd_row.append(self._spd)

        opts.append(fmt_row)
        opts.append(spd_row)
        root.append(opts)

        # Actions
        act = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._play_btn = Gtk.Button()
        self._il(self._play_btn, "media-playback-start-symbolic", "Play")
        self._play_btn.add_css_class("btn-secondary")
        self._play_btn.connect("clicked", self._on_play)

        self._export_btn = Gtk.Button()
        self._export_btn.set_hexpand(True)
        self._il(self._export_btn, "document-save-symbolic", "Export")
        self._export_btn.add_css_class("btn-primary")
        self._export_btn.connect("clicked", self._on_export)

        act.append(self._play_btn)
        act.append(self._export_btn)
        root.append(act)

        # Status
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        self._status = Gtk.Label(label="Ready", xalign=0)
        self._status.add_css_class("export-status")
        self._status.set_hexpand(True)
        status_box.append(self._spinner)
        status_box.append(self._status)
        root.append(status_box)

        self.set_child(root)
        self._update_buttons()

    def _il(self, btn: Gtk.Button, icon: str, label: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        box.set_halign(Gtk.Align.CENTER)
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(14)
        box.append(img)
        box.append(Gtk.Label(label=label))
        btn.set_child(box)

    def load_file(self, path: Path) -> None:
        self._file_lbl.set_text(path.name)
        self._meta_lbl.set_text(self.pw.recorder.get_media_info(path))
        self._status.set_text("File loaded")
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.pw.media_path is not None
        self._play_btn.set_sensitive(has)
        self._export_btn.set_sensitive(has and not self.pw.state.is_recording)

    def _on_open(self, _btn) -> None:
        dlg = Gtk.FileChooserNative.new(
            "Select Media File", self.pw,
            Gtk.FileChooserAction.OPEN, "Open", "Cancel",
        )
        dlg.connect("response", self._file_chosen)
        dlg.show()

    def _file_chosen(self, dlg: Gtk.FileChooserNative, resp: int) -> None:
        if resp == Gtk.ResponseType.ACCEPT:
            f = dlg.get_file()
            if f and f.get_path():
                self.pw.set_media(Path(f.get_path()))
        dlg.destroy()

    def _on_play(self, _btn) -> None:
        if self.pw.media_path:
            Gtk.FileLauncher.new(
                Gio.File.new_for_path(str(self.pw.media_path))
            ).launch(self.pw, None, None, None)

    def _on_export(self, _btn) -> None:
        if not self.pw.media_path:
            return
        fmt_item = self._fmt.get_selected_item()
        spd_item = self._spd.get_selected_item()
        if not fmt_item or not spd_item:
            return
        try:
            speed = float(spd_item.get_string().replace("x", ""))
        except ValueError:
            return

        fmt = fmt_item.get_string()
        path = self.pw.media_path
        out_dir = self.pw.state.output_dir

        self._export_btn.set_sensitive(False)
        self._spinner.set_visible(True)
        self._spinner.start()
        self._status.set_text("Exporting…")

        def run() -> None:
            try:
                result = self.pw.recorder.export_media(path, out_dir, fmt, speed)
                GLib.idle_add(self._export_done, result)
            except RecorderError as exc:
                GLib.idle_add(self._export_failed, str(exc))

        threading.Thread(target=run, daemon=True).start()

    def _export_done(self, path: Path) -> None:
        self._spinner.stop()
        self._spinner.set_visible(False)
        self._status.set_text(f"Saved: {path.name}")
        self.pw.set_media(path)
        self.pw.recordings_popover.refresh()
        self._update_buttons()

    def _export_failed(self, msg: str) -> None:
        self._spinner.stop()
        self._spinner.set_visible(False)
        self._status.set_text("Export failed")
        self._update_buttons()
        self.pw._alert("Export Error", msg)


# ─────────────────────────────────────────────────────────────
#  Settings popover
# ─────────────────────────────────────────────────────────────

class SettingsPopover(Gtk.Popover):
    def __init__(self, parent: "RecorderWindow") -> None:
        super().__init__()
        self.pw = parent
        self.set_size_request(280, -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(14)
        root.set_margin_end(14)

        title = Gtk.Label(label="Settings", xalign=0)
        title.add_css_class("pop-title")
        root.append(title)

        opts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)

        fmt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fmt_row.add_css_class("row-box")
        fl = Gtk.Label(label="Format", xalign=0)
        fl.add_css_class("row-label")
        fl.set_hexpand(True)
        self._fmt = Gtk.DropDown(model=Gtk.StringList.new(["mp4", "mkv", "webm"]))
        self._fmt.connect("notify::selected-item", self._on_fmt)
        fmt_row.append(fl)
        fmt_row.append(self._fmt)

        fps_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fps_row.add_css_class("row-box")
        fpl = Gtk.Label(label="FPS", xalign=0)
        fpl.add_css_class("row-label")
        fpl.set_hexpand(True)
        self._fps = Gtk.DropDown(model=Gtk.StringList.new(["30", "60"]))
        self._fps.connect("notify::selected-item", self._on_fps)
        fps_row.append(fpl)
        fps_row.append(self._fps)

        opts.append(fmt_row)
        opts.append(fps_row)
        root.append(opts)

        # Folder row
        folder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_row.add_css_class("row-box")
        folder_icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        folder_icon.set_pixel_size(14)
        folder_icon.set_opacity(0.55)
        self._folder_lbl = Gtk.Label(xalign=0)
        self._folder_lbl.add_css_class("row-label")
        self._folder_lbl.set_hexpand(True)
        self._folder_lbl.set_ellipsize(3)
        chg = Gtk.Button(label="Change")
        chg.add_css_class("btn-secondary")
        chg.connect("clicked", self._on_change_folder)
        folder_row.append(folder_icon)
        folder_row.append(self._folder_lbl)
        folder_row.append(chg)
        root.append(folder_row)

        self.set_child(root)
        self.refresh()

    def refresh(self) -> None:
        self._folder_lbl.set_text(str(self.pw.state.output_dir))

    def _on_fmt(self, *_) -> None:
        item = self._fmt.get_selected_item()
        if item:
            self.pw.state.output_format = item.get_string()

    def _on_fps(self, *_) -> None:
        item = self._fps.get_selected_item()
        if item:
            self.pw.state.fps = int(item.get_string())

    def _on_change_folder(self, _btn) -> None:
        dlg = Gtk.FileChooserNative.new(
            "Select Output Folder", self.pw,
            Gtk.FileChooserAction.SELECT_FOLDER, "Select", "Cancel",
        )
        dlg.connect("response", self._folder_chosen)
        dlg.show()

    def _folder_chosen(self, dlg, resp) -> None:
        if resp == Gtk.ResponseType.ACCEPT:
            f = dlg.get_file()
            if f and f.get_path():
                self.pw.state.output_dir = Path(f.get_path())
                self.refresh()
                self.pw.recordings_popover.refresh()
        dlg.destroy()


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
        self.media_path: Path | None = None

        self.set_title("ScreenRecorder")
        self.set_default_size(460, 620)
        self.set_resizable(False)
        self._load_css()
        self.set_content(self._build_ui())
        self._refresh_ui()
        self._log.info("Window ready")

    def _load_css(self) -> None:
        p = Gtk.CssProvider()
        p.load_from_path(str(_CSS_PATH))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), p,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # ── Build UI ─────────────────────────────────────────────

    def _build_ui(self) -> Gtk.Widget:
        tv = Adw.ToolbarView()

        # ── HeaderBar ──
        header = Adw.HeaderBar()
        header.add_css_class("flat")

        # Right side: settings, recordings, media tools
        right_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        # Settings button
        settings_btn = Gtk.MenuButton()
        settings_btn.add_css_class("btn-icon-pill")
        settings_btn.set_icon_name("emblem-system-symbolic")
        self.settings_popover = SettingsPopover(self)
        settings_btn.set_popover(self.settings_popover)

        # Recordings button
        rec_btn = Gtk.MenuButton()
        rec_btn.add_css_class("btn-icon-pill")
        rec_btn.set_icon_name("folder-videos-symbolic")
        self.recordings_popover = RecordingsPopover(self)
        rec_btn.set_popover(self.recordings_popover)

        # Media Tools button
        media_btn = Gtk.MenuButton()
        media_btn.add_css_class("btn-icon-pill")
        media_btn.set_icon_name("emblem-photos-symbolic")
        self.media_popover = MediaToolsPopover(self)
        media_btn.set_popover(self.media_popover)

        right_box.append(settings_btn)
        right_box.append(rec_btn)
        right_box.append(media_btn)
        header.pack_end(right_box)

        tv.add_top_bar(header)

        # ── Content ──
        content = self._build_content()
        tv.set_content(content)
        return tv

    def _build_content(self) -> Gtk.Widget:
        # Scrollable center column
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add_css_class("app-bg")
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        col.add_css_class("app-bg")
        col.set_halign(Gtk.Align.FILL)
        col.set_valign(Gtk.Align.FILL)
        col.set_vexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_margin_top(24)
        inner.set_margin_bottom(28)
        inner.set_margin_start(28)
        inner.set_margin_end(28)
        inner.set_size_request(400, -1)

        # Robot icon
        robot_img = self._build_robot_image()
        robot_img.set_margin_bottom(14)
        inner.append(robot_img)

        # App title
        title = Gtk.Label(label="Screen Recorder")
        title.add_css_class("app-title")
        title.set_margin_bottom(24)
        inner.append(title)

        # Main card
        card = self._build_main_card()
        inner.append(card)

        # Status line
        status_row = self._build_status_row()
        status_row.set_margin_top(16)
        inner.append(status_row)

        # Show in Finder button
        self._finder_btn = Gtk.Button()
        self._il(self._finder_btn, "folder-open-symbolic", "Show in Folder")
        self._finder_btn.add_css_class("btn-pill")
        self._finder_btn.set_halign(Gtk.Align.CENTER)
        self._finder_btn.set_margin_top(10)
        self._finder_btn.connect("clicked", self._on_show_folder)
        inner.append(self._finder_btn)

        col.append(inner)
        scroll.set_child(col)
        return scroll

    def _build_robot_image(self) -> Gtk.Widget:
        if _ROBOT_PATH.exists():
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(_ROBOT_PATH), 112, 140, True
                )
                return Gtk.Image.new_from_pixbuf(pb)
            except Exception:
                pass
        # fallback icon
        img = Gtk.Image.new_from_icon_name("media-record-symbolic")
        img.set_pixel_size(96)
        return img

    def _build_main_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("main-card")
        card.set_margin_top(0)
        card.set_margin_bottom(0)
        card.set_margin_start(0)
        card.set_margin_end(0)
        card.set_size_request(400, -1)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_margin_top(16)
        inner.set_margin_bottom(16)
        inner.set_margin_start(16)
        inner.set_margin_end(16)

        # Select Region button
        self._region_btn = Gtk.Button()
        self._il(self._region_btn, "object-select-symbolic", "Select Region")
        self._region_btn.add_css_class("btn-region")
        self._region_btn.connect("clicked", self._on_select_region)
        inner.append(self._region_btn)

        # Resolution pill (shows selected region or full screen size)
        self._res_pill = Gtk.Label(label=self._get_screen_res())
        self._res_pill.add_css_class("res-pill")
        self._res_pill.set_halign(Gtk.Align.CENTER)
        inner.append(self._res_pill)

        # Record / Stop button
        self._record_btn = Gtk.Button()
        self._il(self._record_btn, "media-record-symbolic", "Start Recording")
        self._record_btn.add_css_class("btn-record")
        self._record_btn.connect("clicked", self._on_record_toggle)
        inner.append(self._record_btn)

        card.append(inner)
        return card

    def _build_status_row(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)

        self._status_dot = Gtk.Label(label="●")
        self._status_dot.add_css_class("status-dot-green")
        self._status_dot.set_opacity(0)

        self._status_lbl = Gtk.Label(label="Ready")
        self._status_lbl.add_css_class("status-label")

        self._timer_lbl = Gtk.Label(label="")
        self._timer_lbl.add_css_class("timer-label")

        box.append(self._status_dot)
        box.append(self._status_lbl)
        box.append(self._timer_lbl)
        return box

    def _il(self, btn: Gtk.Button, icon: str, label: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(16)
        box.append(img)
        box.append(Gtk.Label(label=label))
        btn.set_child(box)

    # ── Screen resolution helper ──────────────────────────────

    def _get_screen_res(self) -> str:
        if self.state.selected_region:
            r = self.state.selected_region
            return f"{r.width:,} × {r.height:,}"
        try:
            display = Gdk.Display.get_default()
            monitor = display.get_monitors().get_item(0)
            if monitor:
                geo = monitor.get_geometry()
                return f"{geo.width:,} × {geo.height:,}"
        except Exception:
            pass
        return "Full Screen"

    # ── State refresh ─────────────────────────────────────────

    def _refresh_ui(self) -> None:
        rec = self.state.is_recording

        # Record / Stop button
        if rec:
            self._il(self._record_btn, "media-playback-stop-symbolic", "Stop Recording")
            self._record_btn.remove_css_class("btn-record")
            self._record_btn.add_css_class("btn-stop")
        else:
            self._il(self._record_btn, "media-record-symbolic", "Start Recording")
            self._record_btn.remove_css_class("btn-stop")
            self._record_btn.add_css_class("btn-record")

        self._region_btn.set_sensitive(not rec)
        self._finder_btn.set_sensitive(self.recorder.last_output is not None)

        # Resolution pill
        self._res_pill.set_text(self._get_screen_res())

        if not rec:
            self._status_dot.set_opacity(0)
            self._status_lbl.remove_css_class("recording")
            self._timer_lbl.set_text("")

    # ── Event handlers ────────────────────────────────────────

    def _on_record_toggle(self, _btn: Gtk.Button) -> None:
        if self.state.is_recording:
            self._do_stop()
        else:
            self._do_start_fullscreen()

    def _do_start_fullscreen(self) -> None:
        self.state.selected_region = None
        self._start_recording()

    def _on_select_region(self, _btn: Gtk.Button) -> None:
        if detect_session_type() == "wayland":
            self._alert("Wayland Not Supported",
                        "Region selection is not yet available on Wayland.")
            return
        if shutil.which("slop") is None:
            self._alert("slop Required",
                        "Install slop for region selection:\n  sudo apt install slop")
            return
        self._pick_region()

    def _pick_region(self) -> None:
        self._res_pill.set_text("Drag to select…")
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
        self._res_pill.set_text(f"{region.width:,} × {region.height:,}")
        self._start_recording()

    def _region_cancelled(self) -> None:
        self.state.selected_region = None
        self._res_pill.set_text(self._get_screen_res())
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

        self._status_dot.set_opacity(1.0)
        self._status_dot.remove_css_class("status-dot-green")
        self._status_dot.add_css_class("status-dot-red")
        self._status_lbl.set_text("Recording...")
        self._status_lbl.add_css_class("recording")
        self._refresh_ui()

        self.hide()
        self._show_toolbar()
        self._log.info("Recording → %s", output)

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

        self._status_dot.set_opacity(1.0)
        self._status_dot.remove_css_class("status-dot-red")
        self._status_dot.add_css_class("status-dot-green")
        self._status_lbl.remove_css_class("recording")
        self._status_lbl.set_text(f"Saved: {output.name}")
        self._timer_lbl.set_text("")

        self.recordings_popover.refresh()
        self.set_media(output)
        self._refresh_ui()

    def _on_show_folder(self, _btn: Gtk.Button) -> None:
        target = self.recorder.last_output or self.state.output_dir
        Gtk.FileLauncher.new(
            Gio.File.new_for_path(str(target))
        ).open_containing_folder(self, None, None, None)

    # ── Public helpers for popovers ───────────────────────────

    def set_media(self, path: Path) -> None:
        self.media_path = path
        self.media_popover.load_file(path)

    def open_media_tools(self) -> None:
        # Switch to media popover by finding the media button in header
        # Re-popup is handled by caller
        pass

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
            x = max(0, sw - 340)
            wid = subprocess.run(
                ["xdotool", "search", "--name", "Recorder Controls"],
                capture_output=True, text=True, check=True,
            ).stdout.strip().splitlines()[-1]
            subprocess.run(["wmctrl", "-i", "-r", wid, "-e", f"0,{x},16,-1,-1"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "add,above"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, ValueError, IndexError):
            pass

    # ── Timer ────────────────────────────────────────────────

    def _start_timer(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
        self._timer_id = GLib.timeout_add_seconds(1, self._tick)
        self._tick()

    def _stop_timer(self) -> None:
        if self._timer_id:
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
            self._status_dot.set_opacity(0)
            self._alert("Recording Error", err)
            self._refresh_ui()
            return False
        e = self._elapsed()
        self._timer_lbl.set_text(e)
        if self._toolbar:
            self._toolbar.update(e, True)
        return True

    def _elapsed(self) -> str:
        if not self._rec_start:
            return "00:00"
        s = max(0, int(monotonic() - self._rec_start))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

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
