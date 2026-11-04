$ErrorActionPreference = "Stop"

Write-Host "=========================================="
Write-Host "       GilzaBots - Build do Executavel       "
Write-Host "=========================================="

$VenvScripts = "$PSScriptRoot\.venv\Scripts"
if (Test-Path "$VenvScripts\python.exe") {
    Write-Host "[Ambiente] Ativando virtualenv em $VenvScripts"
    $env:PATH = "$VenvScripts;$env:PATH"
}

Write-Host "`n[1/4] Instalando PyInstaller e dependencias..."
pip install pyinstaller wheel setuptools
pip install ".[gui]"

# The Qt packaged here is the Qt every install.ps1 user gets, so the floor is
# checked against what is actually installed before packaging starts.
Write-Host "`n[1.5/4] Checking the Qt runtime that will be packaged..."
python scripts/check_qt_runtime.py
if ($LASTEXITCODE -ne 0) { throw "The installed Qt is known to break GilzaBots; see the errors above." }

Write-Host "`n[2/4] Construindo o frontend (React/Vite)..."
Push-Location gui_src
try {
    npm install
    npm run build
} finally {
    Pop-Location
}

Write-Host "`n[3/4] Limpando diretórios de build antigos..."
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
# We let PyInstaller handle the dist folder overwriting because Remove-Item fails if File Explorer is open

Write-Host "`n[3.5/4] Baixando Tectonic (Windows) para o empacotamento..."
if (!(Test-Path "bin")) { New-Item -ItemType Directory -Force -Path "bin" | Out-Null }
$tectonicZip = "tectonic-windows.zip"
$tectonicUrl = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-pc-windows-msvc.zip"
Write-Host "Fazendo download de $tectonicUrl"
Invoke-WebRequest -Uri $tectonicUrl -OutFile $tectonicZip
Expand-Archive -Path $tectonicZip -DestinationPath "bin" -Force
Remove-Item -Force $tectonicZip

Write-Host "`n[3.6/4] Baixando Pandoc (Windows) para o empacotamento..."
$pandocVersion = "3.10"
$pandocZip = "pandoc-windows.zip"
$pandocUrl = "https://github.com/jgm/pandoc/releases/download/$pandocVersion/pandoc-$pandocVersion-windows-x86_64.zip"
Write-Host "Fazendo download de $pandocUrl"
Invoke-WebRequest -Uri $pandocUrl -OutFile $pandocZip
$pandocExtractDir = "pandoc_extract"
if (Test-Path $pandocExtractDir) { Remove-Item -Recurse -Force $pandocExtractDir }
Expand-Archive -Path $pandocZip -DestinationPath $pandocExtractDir -Force
$pandocExe = Get-ChildItem -Path $pandocExtractDir -Recurse -Filter "pandoc.exe" | Select-Object -First 1
if ($pandocExe) {
    Copy-Item -Path $pandocExe.FullName -Destination "bin\pandoc.exe" -Force
} else {
    throw "pandoc.exe not found in downloaded archive"
}
Remove-Item -Force $pandocZip
Remove-Item -Recurse -Force $pandocExtractDir

Write-Host "`n[3.7/4] Injetando o cliente OAuth do Google (opcional)..."
# Com GILZABOTS_GDRIVE_CLIENT_ID definido, o build embute o cliente e a
# sincronizacao na nuvem passa a conectar em um clique. Sem ele, o app continua
# funcionando: o usuario cadastra o proprio cliente OAuth nas configuracoes.
# O array vazio some no splat, entao nenhum argumento vazio chega ao PyInstaller.
$googleClientArgs = @()
if ($env:GILZABOTS_GDRIVE_CLIENT_ID) {
    python scripts/embed_google_client.py --from-env
    $googleClientArgs = @("--add-data=gilzabots/cloud/providers/bundled_google_client.json;gilzabots/cloud/providers")
} else {
    Write-Host "GILZABOTS_GDRIVE_CLIENT_ID nao definido - build sem cliente OAuth embutido."
}

Write-Host "`n[4/4] Empacotando com PyInstaller..."
# Find winpty-agent.exe dynamically to avoid hardcoding .venv path
$winptyAgentPath = python -c "import winpty, os; print(os.path.join(os.path.dirname(winpty.__file__), 'winpty-agent.exe'))"

# A sintaxe de --add-data no Windows usa ponto-e-virgula (;)
# Qt is collected by PyInstaller's own PyQt6 hooks, which take only the modules
# the application imports (plus QtWebEngineProcess, its resources and every
# locale). --collect-all "PyQt6" would copy the whole wheel instead. The QML
# bindings are excluded because the window is widgets-based: QtWebEngineCore
# links Qt6Qml/Qt6Quick (still bundled through that dependency), but the QtQml
# hook would add every QML plugin and, through them, Quick3D, QuickControls2,
# Multimedia and more. See PROJECT_DESIGN.md §2.16.1.
pyinstaller --name "GilzaBots" `
            @googleClientArgs `
            --windowed `
            --icon="AppIcons/GilzaBots.ico" `
            --add-data="gilzabots/gui;gilzabots/gui" `
            --add-data="gilzabots/icon.png;gilzabots" `
            --add-data="icon.png;." `
            --add-data="gilzabots/assetstore;gilzabots/assetstore" `
            --add-data="gilzabots/templates;gilzabots/templates" `
            --add-data="gilzabots/guides;gilzabots/guides" `
            --add-data="bin;bin" `
            --add-data="config.yaml;." `
            --add-data="skills;skills" `
            --add-data="templates;templates" `
            --add-data="uninstall.ps1;." `
            --collect-all "litellm" `
            --collect-all "tiktoken" `
            --collect-all "tiktoken_ext" `
            --copy-metadata "tiktoken" `
            --collect-all "certifi" `
            --collect-all "httpx" `
            --collect-all "aiohttp" `
            --collect-all "requests" `
            --collect-all "chromadb" `
            --collect-all "duckduckgo_search" `
            --collect-all "instructor" `
            --collect-all "agenticblocks" `
            --collect-all "webview" `
            --collect-all "pythonnet" `
            --collect-all "clr_loader" `
            --exclude-module "PyQt6.QtQml" `
            --exclude-module "PyQt6.QtQuick" `
            --exclude-module "PyQt6.QtQuickWidgets" `
            --collect-all "winpty" `
            --collect-all "docx" `
            --collect-all "pptx" `
            --collect-all "xlsxwriter" `
            --collect-all "pymupdf" `
            --collect-all "pymupdf4llm" `
            --collect-all "tree_sitter" `
            --collect-all "tree_sitter_language_pack" `
            --add-binary "$winptyAgentPath;winpty" `
            --noconfirm `
            --clean `
            main.py

Write-Host "`n=========================================="
Write-Host "Build concluido com sucesso!"
Write-Host "O executavel pode ser encontrado em: .\dist\GilzaBots\GilzaBots.exe"
Write-Host "=========================================="
