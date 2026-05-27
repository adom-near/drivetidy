# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the DriveTidy single-file binary used in the
# "DriveTidy" internal distribution.
#
# Usage:
#   pyinstaller --clean --noconfirm drivetidy.spec
#
# Produces:
#   dist/drivetidy            ← single-file standalone binary (no Python install needed)

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# FastAPI / Uvicorn / Starlette have a lot of dynamic imports that
# PyInstaller's static analysis misses. Force-collect them.
hidden = []
hidden += collect_submodules('fastapi')
hidden += collect_submodules('starlette')
hidden += collect_submodules('uvicorn')
hidden += collect_submodules('pydantic')
hidden += collect_submodules('pydantic_core')
hidden += collect_submodules('anyio')
hidden += collect_submodules('h11')

# uvicorn loads its lifespan / loops / protocols by string name at runtime.
# These are the most common ones missed by analysis.
hidden += [
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    'uvicorn.loops.auto',
    'uvicorn.loops.asyncio',
    'uvicorn.loops.uvloop',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.http.httptools_impl',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.protocols.websockets.websockets_impl',
    'uvicorn.protocols.websockets.wsproto_impl',
    'uvicorn.logging',
]

# Static / template assets shipped inside the GUI package have to be
# packaged as data, not code, since they're read at runtime via Path().
datas = [
    ('drivetidy/gui/templates', 'drivetidy/gui/templates'),
    ('drivetidy/gui/static', 'drivetidy/gui/static'),
]
# Some Pydantic builds load schema files at runtime.
datas += collect_data_files('pydantic')


a = Analysis(
    ['scripts/pyinstaller_entry.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # We don't need these GUI/stdlib bits — keep the bundle small.
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='drivetidy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,   # match host (arm64 on this build machine)
    codesign_identity=None,
    entitlements_file=None,
)
