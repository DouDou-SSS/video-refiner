#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$ROOT_DIR/dist_portable"
PACKAGE_DIR="$BUILD_DIR/video-refiner"
ZIP_PATH="$BUILD_DIR/video-refiner-portable.zip"
CAMOUFOX_CACHE_SRC="${VIDEO_REFINER_CAMOUFOX_CACHE:-$HOME/Library/Caches/camoufox}"
export COPYFILE_DISABLE=1

log() {
  echo "==> $1"
}

require_file() {
  if [[ ! -e "$1" ]]; then
    echo "缺少必要文件：$1"
    exit 1
  fi
}

cd "$ROOT_DIR"

log "检查必要文件"
require_file "$ROOT_DIR/install.command"
require_file "$ROOT_DIR/start.command"
require_file "$ROOT_DIR/README-给使用者.md"
require_file "$ROOT_DIR/config.example.yaml"
require_file "$ROOT_DIR/models/whisper/faster-whisper-tiny/model.bin"
require_file "$ROOT_DIR/models/whisper/faster-whisper-tiny/config.json"
require_file "$ROOT_DIR/models/whisper/faster-whisper-tiny/tokenizer.json"
require_file "$ROOT_DIR/prompts"
require_file "$ROOT_DIR/webapp/backend/videorefiner_app"
require_file "$ROOT_DIR/webapp/backend/requirements.txt"
require_file "$CAMOUFOX_CACHE_SRC/Camoufox.app"

if [[ ! -f "$ROOT_DIR/webapp/frontend/dist/index.html" ]]; then
  if [[ ! -f "$ROOT_DIR/webapp/frontend/package.json" ]]; then
    echo "缺少前端 dist，且没有前端源码可构建。"
    exit 1
  fi
  if ! command -v npm >/dev/null 2>&1; then
    echo "需要 npm 来构建前端 dist。"
    exit 1
  fi
  log "构建前端"
  (cd "$ROOT_DIR/webapp/frontend" && npm install && npm run build)
fi

log "创建干净分发目录"
rm -rf "$BUILD_DIR"
mkdir -p "$PACKAGE_DIR/webapp/backend" "$PACKAGE_DIR/webapp/frontend" "$PACKAGE_DIR/models/whisper"
mkdir -p "$PACKAGE_DIR/vendor/camoufox"

rsync -a "$ROOT_DIR/install.command" "$PACKAGE_DIR/"
rsync -a "$ROOT_DIR/start.command" "$PACKAGE_DIR/"
rsync -a "$ROOT_DIR/README-给使用者.md" "$PACKAGE_DIR/"
rsync -a "$ROOT_DIR/config.example.yaml" "$PACKAGE_DIR/"
rsync -a \
  --exclude ".cache" \
  --exclude "._*" \
  --exclude ".DS_Store" \
  "$ROOT_DIR/models/whisper/faster-whisper-tiny" "$PACKAGE_DIR/models/whisper/"
rsync -a "$ROOT_DIR/prompts" "$PACKAGE_DIR/"
rsync -a "$ROOT_DIR/webapp/backend/requirements.txt" "$PACKAGE_DIR/webapp/backend/"
rsync -a \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude "._*" \
  --exclude ".DS_Store" \
  "$ROOT_DIR/webapp/backend/videorefiner_app" "$PACKAGE_DIR/webapp/backend/"
rsync -a "$ROOT_DIR/webapp/frontend/dist" "$PACKAGE_DIR/webapp/frontend/"
rsync -a "$CAMOUFOX_CACHE_SRC/Camoufox.app" "$PACKAGE_DIR/vendor/camoufox/"
if [[ -f "$CAMOUFOX_CACHE_SRC/version.json" ]]; then
  rsync -a "$CAMOUFOX_CACHE_SRC/version.json" "$PACKAGE_DIR/vendor/camoufox/"
fi

chmod +x "$PACKAGE_DIR/install.command" "$PACKAGE_DIR/start.command"

log "隐私扫描"
if rg -n "(doudou|\\.openclaw|sk-proj-[A-Za-z0-9_-]{20,}|sk-sp-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,})" "$PACKAGE_DIR"; then
  echo "分发目录中发现疑似隐私或密钥内容，已停止打包。"
  exit 1
fi

log "生成压缩包"
find "$PACKAGE_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$PACKAGE_DIR" \( -name "*.pyc" -o -name "._*" -o -name ".DS_Store" \) -delete
(cd "$BUILD_DIR" && ditto --norsrc -c -k --keepParent "video-refiner" "$ZIP_PATH")

echo
echo "已生成：$ZIP_PATH"
echo "可以把这个 zip 发给其他 Mac 用户。"
