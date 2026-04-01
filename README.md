# Screen Recorder

A fast, modern desktop screen recorder for Linux built with GTK4 and Libadwaita.

![Screen Recorder](assets/icons/screen-recorder-128.png)

## Features

- **Full-screen recording** — one click to start capturing
- **Region selection** — drag to select any area of the screen (requires `slop`)
- **Multiple formats** — record in MP4, MKV, or WebM
- **FPS control** — 30 or 60 frames per second
- **Media Tools** — convert between MP4, MKV, WebM, GIF and adjust playback speed (0.5× – 4×)
- **Recording library** — browse, preview and manage all your recordings in one place
- **Floating toolbar** — minimal controls shown during recording without cluttering your screen
- **Dark theme** — modern dark UI built on Libadwaita

## Requirements

### Runtime
- Python 3.11+
- GTK 4
- Libadwaita
- FFmpeg

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 ffmpeg
```

### Region selection (optional)
```bash
sudo apt install slop
```

### Toolbar positioning (optional, X11 only)
```bash
sudo apt install xdotool wmctrl
```

## Installation

### Run from source

```bash
git clone https://github.com/palamut62/screen-recorder.git
cd screen-recorder
python3 -m app.main
```

### Pre-built binary

Download the latest binary from the [Releases](../../releases) page:

```bash
chmod +x screen-recorder
./screen-recorder
```

### Install desktop entry

```bash
cp screen-recorder.desktop ~/.local/share/applications/
# Edit Exec= path to match your installation directory
```

## Building from source

```bash
pip3 install pyinstaller
pyinstaller screen-recorder.spec --clean --noconfirm
# Binary will be in dist/screen-recorder
```

## Usage

1. **Full Screen** — click "Full Screen" to start recording immediately
2. **Region** — click "Select Region", drag on screen to select area (requires `slop`)
3. **Stop** — use the floating toolbar or click "Stop Recording" in the main window
4. **Export** — switch to "Media Tools" tab, select a file from the library, choose format and speed, click Export

## Project Structure

```
app/
├── main.py              # Entry point
├── core/
│   └── state.py         # Application state (AppState, Region)
├── recorder/
│   └── ffmpeg.py        # FFmpeg wrapper (recording, export, media info)
├── ui/
│   ├── window.py        # GTK4 / Libadwaita UI
│   └── style.css        # Custom dark theme
└── utils/
    ├── env.py           # X11/Wayland detection
    └── logging.py       # File-based logging
assets/
└── icons/               # App icons (SVG + PNG at 16–512px + ICO)
dist/
└── screen-recorder      # Compiled Linux binary
```

## Wayland

Region selection is not yet supported on Wayland. Full-screen recording on Wayland requires PipeWire + XDG Desktop Portal integration, which is planned for a future release.

## License

MIT
