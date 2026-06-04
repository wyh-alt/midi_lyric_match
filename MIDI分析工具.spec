# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['midi_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'numpy', 'soundfile', 'sounddevice', '_soundfile_data',
        'PyQt6', 'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets',
        'qfluentwidgets', 'qframelesswindow',
        'midi_lyric_aligner', 'lyric_calibrator_gui',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MIDI分析工具',
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
    icon=['icon.ico'],
)
