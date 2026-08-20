# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

# Bundle the FFmpeg binary used for optional hardware decoding.
try:
    from imageio_ffmpeg import get_ffmpeg_exe
    ffmpeg_binary = get_ffmpeg_exe()
except Exception:
    ffmpeg_binary = None

# Bundle the pyMFT DLL if it has been built.
pymft_dll = os.path.join(os.path.abspath(SPECPATH), 'pyMFT', 'pyMFT.dll')
pymft_dll = pymft_dll if os.path.exists(pymft_dll) else None
print(f"pyMFT DLL path: {pymft_dll}")

datas = []
binaries = []
if ffmpeg_binary:
    binaries.append((ffmpeg_binary, '.'))
if pymft_dll:
    # Place the DLL next to the pyMFT Python package so _find_dll() sees it.
    datas.append((pymft_dll, 'pyMFT'))

hiddenimports = ['PIL.Image', 'win32event', 'pyMFT', 'pyMFT.decoder']
for pkg in ('pystray', 'av', 'imageio_ffmpeg'):
    tmp_ret = collect_all(pkg)
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]


a = Analysis(
    ['companion_ui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='BestCam',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='BestCam',
)
