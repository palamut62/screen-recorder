# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['app/main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('app/ui/style.css', 'app/ui'),
        ('assets/icons', 'assets/icons'),
    ],
    hiddenimports=[
        'gi',
        'gi.repository.Gtk',
        'gi.repository.Adw',
        'gi.repository.Gdk',
        'gi.repository.Gio',
        'gi.repository.GLib',
        'gi.repository.GObject',
        'app.core.state',
        'app.recorder.ffmpeg',
        'app.ui.window',
        'app.utils.env',
        'app.utils.logging',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='screen-recorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icons/screen-recorder.ico',
)
