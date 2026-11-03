#!/usr/bin/env bash
set -e

echo "=========================================="
echo "       GilzaBots - Build do Executavel       "
echo "=========================================="

echo -e "\n[1/4] Instalando dependencias e PyInstaller..."
pip install pyinstaller wheel setuptools
pip install ".[gui]"

# The Qt packaged here is the Qt every install.sh user gets, so the floor is
# checked against what is actually installed before packaging starts.
echo -e "
[1.5/4] Checking the Qt runtime that will be packaged..."
python scripts/check_qt_runtime.py

echo -e "\n[2/4] Construindo o frontend (React/Vite)..."
pushd gui_src > /dev/null
npm install
npm run build
popd > /dev/null

echo -e "\n[3/4] Limpando diretorios de build antigos..."
rm -rf build

echo -e "\n[3.5/4] Baixando Tectonic para o empacotamento..."
mkdir -p bin
OS_NAME=$(uname -s)
ARCH_NAME=$(uname -m)

if [ "$OS_NAME" = "Darwin" ]; then
    if [ "$ARCH_NAME" = "arm64" ]; then
        TECTONIC_URL="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-aarch64-apple-darwin.tar.gz"
    else
        TECTONIC_URL="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-apple-darwin.tar.gz"
    fi
else
    # Assuming Linux x86_64
    TECTONIC_URL="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-unknown-linux-gnu.tar.gz"
fi

echo "Fazendo download de $TECTONIC_URL"
curl -L $TECTONIC_URL -o tectonic.tar.gz
tar -xzf tectonic.tar.gz -C bin
rm tectonic.tar.gz

echo -e "\n[3.6/4] Baixando Pandoc para o empacotamento..."
PANDOC_VERSION="3.10"
if [ "$OS_NAME" = "Darwin" ]; then
    if [ "$ARCH_NAME" = "arm64" ]; then
        PANDOC_URL="https://github.com/jgm/pandoc/releases/download/$PANDOC_VERSION/pandoc-$PANDOC_VERSION-arm64-macOS.zip"
    else
        PANDOC_URL="https://github.com/jgm/pandoc/releases/download/$PANDOC_VERSION/pandoc-$PANDOC_VERSION-x86_64-macOS.zip"
    fi
else
    PANDOC_URL="https://github.com/jgm/pandoc/releases/download/$PANDOC_VERSION/pandoc-$PANDOC_VERSION-linux-amd64.tar.gz"
fi

echo "Fazendo download de $PANDOC_URL"
if [[ "$PANDOC_URL" == *.zip ]]; then
    curl -L "$PANDOC_URL" -o pandoc.zip
    rm -rf pandoc_extract
    mkdir -p pandoc_extract
    python -m zipfile -e pandoc.zip pandoc_extract
    PANDOC_BIN=$(find pandoc_extract -type f -name pandoc | head -n 1)
    if [ -z "$PANDOC_BIN" ]; then
        echo "pandoc not found in downloaded archive"
        exit 1
    fi
    cp "$PANDOC_BIN" bin/pandoc
    rm -rf pandoc_extract pandoc.zip
else
    curl -L "$PANDOC_URL" -o pandoc.tar.gz
    rm -rf pandoc_extract
    mkdir -p pandoc_extract
    tar -xzf pandoc.tar.gz -C pandoc_extract
    PANDOC_BIN=$(find pandoc_extract -type f -name pandoc | head -n 1)
    if [ -z "$PANDOC_BIN" ]; then
        echo "pandoc not found in downloaded archive"
        exit 1
    fi
    cp "$PANDOC_BIN" bin/pandoc
    rm -rf pandoc_extract pandoc.tar.gz
fi
chmod +x bin/pandoc

echo -e "\n[3.7/4] Injetando o cliente OAuth do Google (opcional)..."
# Com GILZABOTS_GDRIVE_CLIENT_ID definido, o build embute o cliente e a
# sincronizacao na nuvem passa a conectar em um clique. Sem ele, o app continua
# funcionando: o usuario cadastra o proprio cliente OAuth nas configuracoes.
GOOGLE_CLIENT_DATA=""
if [ -n "$GILZABOTS_GDRIVE_CLIENT_ID" ]; then
    python scripts/embed_google_client.py --from-env
    GOOGLE_CLIENT_DATA="--add-data=gilzabots/cloud/providers/bundled_google_client.json:gilzabots/cloud/providers"
else
    echo "GILZABOTS_GDRIVE_CLIENT_ID nao definido - build sem cliente OAuth embutido."
fi

echo -e "\n[4/4] Empacotando com PyInstaller..."
# A sintaxe de --add-data no Linux/macOS usa dois pontos (:)
# Qt is collected by PyInstaller's own PyQt6 hooks, which take only the modules
# the application imports (plus QtWebEngineProcess, its resources and every
# locale). --collect-all "PyQt6" would copy the whole wheel instead. The QML
# bindings are excluded because the window is widgets-based: QtWebEngineCore
# links libQt6Qml/libQt6Quick (still bundled through that dependency), but the
# QtQml hook would add every QML plugin and, through them, Quick3D,
# QuickControls2, Multimedia and more. See PROJECT_DESIGN.md §2.16.1.
pyinstaller --name "GilzaBots" \
            $GOOGLE_CLIENT_DATA \
            --windowed \
            --icon="AppIcons/GilzaBots.ico" \
            --add-data="gilzabots/gui:gilzabots/gui" \
            --add-data="gilzabots/icon.png:gilzabots" \
            --add-data="icon.png:." \
            --add-data="gilzabots/assetstore:gilzabots/assetstore" \
            --add-data="gilzabots/templates:gilzabots/templates" \
            --add-data="gilzabots/guides:gilzabots/guides" \
            --add-data="bin:bin" \
            --add-data="config.yaml:." \
            --add-data="skills:skills" \
            --add-data="templates:templates" \
            --add-data="uninstall.sh:." \
            --collect-all "litellm" \
            --collect-all "tiktoken" \
            --collect-all "tiktoken_ext" \
            --copy-metadata "tiktoken" \
            --collect-all "certifi" \
            --collect-all "httpx" \
            --collect-all "aiohttp" \
            --collect-all "requests" \
            --collect-all "chromadb" \
            --collect-all "duckduckgo_search" \
            --collect-all "instructor" \
            --collect-all "agenticblocks" \
            --collect-all "webview" \
            --collect-all "pythonnet" \
            --collect-all "clr_loader" \
            --exclude-module "PyQt6.QtQml" \
            --exclude-module "PyQt6.QtQuick" \
            --exclude-module "PyQt6.QtQuickWidgets" \
            --collect-all "docx" \
            --collect-all "pptx" \
            --collect-all "xlsxwriter" \
            --collect-all "pymupdf" \
            --collect-all "pymupdf4llm" \
            --collect-all "tree_sitter" \
            --collect-all "tree_sitter_language_pack" \
            --noconfirm \
            --clean \
            main.py

if [ "$OS_NAME" = "Linux" ]; then
    # PyInstaller only warns when a dependency is missing from this machine, and
    # the bundle then starts only where the user has it installed.
    echo -e "\n[4.5/4] Checking that the bundle carries the libraries the window needs..."
    python scripts/check_bundle_libraries.py dist/GilzaBots
fi

echo -e "\n=========================================="
echo "Build concluido com sucesso!"
echo "O executavel pode ser encontrado em: ./dist/GilzaBots/GilzaBots"
echo "=========================================="
