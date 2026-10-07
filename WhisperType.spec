# WhisperType.spec
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['src/main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Sent into the server container as its launcher; see server_manager.
        ('src/server_patch.py', '.'),
        # The phrase the health probe speaks; see server_health.
        ('src/assets/health_probe.wav', 'assets'),
    ],
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
