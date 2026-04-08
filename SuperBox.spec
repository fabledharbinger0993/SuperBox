# -*- mode: python ; coding: utf-8 -*-
#
# SuperBox PyInstaller spec
#
# Build with:  bash build.sh
# Output:      dist/SuperBox.app
#
# The bundle is self-contained — Python, all pip dependencies, templates, and
# static assets are packed inside.  No Python or Homebrew needed on the user's
# machine (ffmpeg/chromaprint are still required for audio analysis features;
# see README).

from pathlib import Path

SRC = Path('.')  # run PyInstaller from the SuperBox/ directory

a = Analysis(
    [str(SRC / 'main.py')],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        # Flask templates and static assets must travel with the bundle
        (str(SRC / 'templates'), 'templates'),
        (str(SRC / 'static'),    'static'),
    ],
    hiddenimports=[
        # Waitress imports these dynamically
        'waitress',
        'waitress.task',
        'waitress.channel',
        'waitress.server',
        'waitress.runner',
        'waitress.utilities',
        # Flask / Jinja2 internals
        'flask',
        'flask.templating',
        'jinja2',
        'jinja2.ext',
        # pkg_resources used by several deps
        'pkg_resources',
        'pkg_resources.py2_compat',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',    # not needed — pywebview uses native WKWebView
        'matplotlib', # heavy and unused
        'IPython',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SuperBox',
    debug=False,
    strip=False,
    upx=True,
    console=False,           # no terminal window
    argv_emulation=False,
    target_arch=None,        # universal2 can be forced here if needed
    codesign_identity=None,  # set to your Apple Developer ID to code-sign
    entitlements_file=None,
    icon=str(SRC / 'static' / 'SuperBox.icns'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SuperBox',
)

app = BUNDLE(
    coll,
    name='SuperBox.app',
    icon=str(SRC / 'static' / 'SuperBox.icns'),
    bundle_identifier='com.fabledharbinger.superbox',
    info_plist={
        'CFBundleName':             'SuperBox',
        'CFBundleDisplayName':      'SuperBox',
        'CFBundleShortVersionString': '1.0.0',
        'NSPrincipalClass':         'NSApplication',
        'NSHighResolutionCapable':  True,
        # Allow WKWebView to connect to the local Flask server
        'NSAppTransportSecurity': {
            'NSAllowsLocalNetworking': True,
        },
        'LSMinimumSystemVersion': '12.0',
    },
)
