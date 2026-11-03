# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

datas = [('gilzabots/gui', 'gilzabots/gui'), ('gilzabots/icon.png', 'gilzabots'), ('icon.png', '.'), ('gilzabots/assetstore', 'gilzabots/assetstore'), ('gilzabots/templates', 'gilzabots/templates'), ('gilzabots/guides', 'gilzabots/guides'), ('bin', 'bin'), ('config.yaml', '.'), ('skills', 'skills'), ('templates', 'templates'), ('uninstall.sh', '.'), ('uninstall.ps1', '.')]
binaries = [('C:\\Users\\gilza\\projetos\\GilzaBots\\.venv\\Lib\\site-packages\\winpty\\winpty-agent.exe', 'winpty')]
hiddenimports = []
# Cloud storage backends are imported lazily by gilzabots.cloud.registry so that a
# missing optional dependency cannot break start-up. Naming them here keeps the
# frozen build from dropping a module the import graph only reaches at runtime.
hiddenimports += [
    'gilzabots.cloud',
    'gilzabots.cloud.chats',
    'gilzabots.cloud.engine',
    'gilzabots.cloud.oauth',
    'gilzabots.cloud.registry',
    'gilzabots.cloud.scanner',
    'gilzabots.cloud.service',
    'gilzabots.cloud.state',
    'gilzabots.cloud.providers',
    'gilzabots.cloud.providers.google_drive',
    'gilzabots.cloud.providers.local_folder',
]
# The Google OAuth client is injected before packaging by
# scripts/embed_google_client.py and is absent from the source tree, so it is
# added only when it exists — PyInstaller errors out on a missing data file.
import os as _os
_bundled_google_client = _os.path.join('gilzabots', 'cloud', 'providers', 'bundled_google_client.json')
if _os.path.exists(_bundled_google_client):
    datas += [(_bundled_google_client, 'gilzabots/cloud/providers')]

datas += copy_metadata('tiktoken')
tmp_ret = collect_all('litellm')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tiktoken')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tiktoken_ext')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('certifi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('httpx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('aiohttp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('requests')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('chromadb')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('duckduckgo_search')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('instructor')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('agenticblocks')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pythonnet')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('clr_loader')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PyQt6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PyQt6-WebEngine')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('winpty')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('docx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pptx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('xlsxwriter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pymupdf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pymupdf4llm')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tree_sitter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tree_sitter_language_pack')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
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
    name='GilzaBots',
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
    icon=['AppIcons\\GilzaBots.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='GilzaBots',
)
