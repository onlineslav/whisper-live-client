# WhisperType.spec
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['src/main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'pynput.keyboard._win32', 
        'pynput.mouse._win32'
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    name='WhisperType',
    console=False, # Create a windowed app
    icon=None,
)
