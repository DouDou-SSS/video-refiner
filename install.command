#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_HOME="${VIDEO_REFINER_HOME:-$HOME/.video-refiner}"
BACKEND_DIR="$ROOT_DIR/webapp/backend"
FRONTEND_DIR="$ROOT_DIR/webapp/frontend"
NODE_TOOLS_DIR="$ROOT_DIR/webapp/node-tools"
VENV_DIR="$BACKEND_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
CONFIG_PATH="${VIDEO_REFINER_CONFIG:-$APP_HOME/config.yaml}"
WHISPER_MODEL_DIR="$ROOT_DIR/models/whisper/faster-whisper-tiny"
CAMOUFOX_VENDOR_DIR="$ROOT_DIR/vendor/camoufox"
CAMOUFOX_CACHE_DIR="$HOME/Library/Caches/camoufox"
HOST_PYTHON=""
export PATH="$NODE_TOOLS_DIR/node_modules/.bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

pause_on_exit() {
  local code=$?
  if [[ -t 0 && "${VIDEO_REFINER_NO_PAUSE:-0}" != "1" ]]; then
    echo
    if [[ $code -eq 0 ]]; then
      echo "安装完成。按回车关闭窗口。"
    else
      echo "安装失败，退出码：$code。请查看上面的错误信息，按回车关闭窗口。"
    fi
    read -r
  fi
  exit $code
}
trap pause_on_exit EXIT

log() {
  echo "==> $1"
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-y}"
  local answer
  if [[ ! -t 0 ]]; then
    [[ "$default" == "y" ]]
    return
  fi
  if [[ "$default" == "y" ]]; then
    read -r "?$prompt [Y/n] " answer
    [[ -z "$answer" || "$answer" =~ ^[Yy]$ ]]
  else
    read -r "?$prompt [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]]
  fi
}

is_compatible_python() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
major, minor = sys.version_info[:2]
if major != 3 or minor < 10 or minor > 12:
    raise SystemExit(1)
PY
}

find_compatible_python() {
  local candidates=(
    "${VIDEO_REFINER_PYTHON:-}"
    python3.12
    python3.11
    python3.10
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.11
    /opt/homebrew/bin/python3.10
    /usr/local/bin/python3.12
    /usr/local/bin/python3.11
    /usr/local/bin/python3.10
    python3
  )
  local candidate resolved
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" ]] || continue
    if [[ "$candidate" == */* ]]; then
      [[ -x "$candidate" ]] || continue
      resolved="$candidate"
    else
      resolved="$(command -v "$candidate" 2>/dev/null || true)"
      [[ -n "$resolved" ]] || continue
    fi
    if is_compatible_python "$resolved"; then
      echo "$resolved"
      return 0
    fi
  done

  echo "未找到兼容 Python。需要 Python 3.10 到 3.12；当前 python3 可能太新或太旧。" >&2
  if command -v brew >/dev/null 2>&1 && ask_yes_no "是否使用 Homebrew 安装 python@3.12？" "y"; then
    brew install python@3.12
    rehash
    for candidate in /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12 python3.12; do
      if [[ "$candidate" == */* ]]; then
        [[ -x "$candidate" ]] || continue
        resolved="$candidate"
      else
        resolved="$(command -v "$candidate" 2>/dev/null || true)"
        [[ -n "$resolved" ]] || continue
      fi
      if is_compatible_python "$resolved"; then
        echo "$resolved"
        return 0
      fi
    done
  fi

  echo "请安装 Python 3.10 到 3.12 后重试。推荐：brew install python@3.12" >&2
  exit 1
}

prepare_venv() {
  if [[ -x "$PYTHON_BIN" ]] && ! is_compatible_python "$PYTHON_BIN"; then
    echo "检测到已有虚拟环境 Python 版本不兼容，将重建：$($PYTHON_BIN --version 2>&1)"
    rm -rf "$VENV_DIR"
  fi
  "$HOST_PYTHON" -m venv "$VENV_DIR"
}

install_ffmpeg_if_needed() {
  if command -v ffmpeg >/dev/null 2>&1; then
    return
  fi
  echo "未找到 ffmpeg。抽帧、字幕和转写都需要 ffmpeg。"
  if command -v brew >/dev/null 2>&1 && ask_yes_no "是否使用 Homebrew 安装 ffmpeg？" "y"; then
    brew install ffmpeg
    rehash
  else
    echo "请安装 ffmpeg 后再运行软件：brew install ffmpeg"
    exit 1
  fi
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg 仍不可用，请确认 Homebrew 路径已加入 PATH。"
    exit 1
  fi
}

ensure_npm() {
  if command -v npm >/dev/null 2>&1; then
    return
  fi
  echo "未找到 npm。mcporter 和 OpenCLI 现在是必需组件，需要 Node.js/npm。"
  if command -v brew >/dev/null 2>&1; then
    echo "使用 Homebrew 安装 node。"
    brew install node
    rehash
  else
    echo "请先安装 Node.js，或安装 Homebrew 后重试。"
    exit 1
  fi
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm 仍不可用，请确认 Node.js 安装成功。"
    exit 1
  fi
}

install_required_node_tools() {
  ensure_npm
  mkdir -p "$NODE_TOOLS_DIR"
  log "安装必需下载组件：mcporter、OpenCLI"
  npm install --prefix "$NODE_TOOLS_DIR" mcporter @jackwener/opencli
  if ! command -v mcporter >/dev/null 2>&1; then
    echo "mcporter 安装后仍不可用。"
    exit 1
  fi
  if ! command -v opencli >/dev/null 2>&1; then
    echo "OpenCLI 安装后仍不可用。"
    exit 1
  fi
  echo "mcporter: $(mcporter --version 2>/dev/null || echo installed)"
  echo "OpenCLI: $(opencli --version 2>/dev/null || echo installed)"
}

verify_runtime_imports() {
  "$PYTHON_BIN" - <<'PY'
import importlib.util
import sys

required = [
    "fastapi",
    "uvicorn",
    "openai",
    "yaml",
    "faster_whisper",
    "rapidocr_onnxruntime",
    "yt_dlp",
    "torch",
    "torchaudio",
    "funasr",
    "camoufox",
]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit("这些运行依赖没有安装成功：" + "、".join(missing))

print("运行依赖检查通过：" + sys.executable)
PY
	}

ensure_whisper_model_cache() {
  local missing=0
  for file in model.bin config.json tokenizer.json; do
    [[ -f "$WHISPER_MODEL_DIR/$file" ]] || missing=1
  done
  if [[ "$missing" -eq 0 ]]; then
    echo "Whisper 本地模型已就绪：$WHISPER_MODEL_DIR"
    return
  fi

  log "下载 Whisper 本地模型缓存：faster-whisper-tiny"
  mkdir -p "$WHISPER_MODEL_DIR"
  "$PYTHON_BIN" - <<'PY'
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="Systran/faster-whisper-tiny",
    local_dir="models/whisper/faster-whisper-tiny",
    allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"],
)
PY

  for file in model.bin config.json tokenizer.json; do
    if [[ ! -f "$WHISPER_MODEL_DIR/$file" ]]; then
      echo "Whisper 模型下载不完整，缺少：$WHISPER_MODEL_DIR/$file"
      exit 1
    fi
  done
}

install_camoufox_browsers() {
  log "安装 Camoufox 浏览器运行文件"
  if [[ -d "$CAMOUFOX_CACHE_DIR/Camoufox.app" ]]; then
    echo "Camoufox 浏览器运行文件已就绪：$CAMOUFOX_CACHE_DIR/Camoufox.app"
    return
  fi
  if [[ -d "$CAMOUFOX_VENDOR_DIR/Camoufox.app" ]]; then
    mkdir -p "$CAMOUFOX_CACHE_DIR"
    rsync -a "$CAMOUFOX_VENDOR_DIR/Camoufox.app" "$CAMOUFOX_CACHE_DIR/"
    if [[ -f "$CAMOUFOX_VENDOR_DIR/version.json" ]]; then
      rsync -a "$CAMOUFOX_VENDOR_DIR/version.json" "$CAMOUFOX_CACHE_DIR/"
    fi
    echo "已从软件包内置缓存安装 Camoufox：$CAMOUFOX_CACHE_DIR/Camoufox.app"
    return
  fi
  if ! "$PYTHON_BIN" -m camoufox fetch; then
    echo "Camoufox 浏览器运行文件安装失败。请检查网络能否访问 GitHub，或使用包含 vendor/camoufox/Camoufox.app 的完整软件包。"
    exit 1
  fi
}

build_frontend_if_needed() {
  if [[ -f "$FRONTEND_DIR/dist/index.html" ]]; then
    return
  fi
  if [[ ! -f "$FRONTEND_DIR/package.json" ]]; then
    echo "缺少前端构建产物：$FRONTEND_DIR/dist/index.html"
    echo "这个软件包不完整，请重新获取完整压缩包。"
    exit 1
  fi
  if ! command -v npm >/dev/null 2>&1; then
    echo "前端还未构建，但当前 Mac 未安装 npm。请安装 Node.js 后重试。"
    exit 1
  fi
  log "构建前端页面"
  (cd "$FRONTEND_DIR" && npm install && npm run build)
}

write_config() {
  mkdir -p "$APP_HOME"
  export VIDEO_REFINER_INSTALL_ROOT="$ROOT_DIR"
  export VIDEO_REFINER_INSTALL_APP_HOME="$APP_HOME"
  export VIDEO_REFINER_INSTALL_CONFIG="$CONFIG_PATH"
  export VIDEO_REFINER_INSTALL_PYTHON="$PYTHON_BIN"
  export VIDEO_REFINER_INSTALL_FFMPEG="$(command -v ffmpeg || echo ffmpeg)"
  "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path
import yaml

root = Path(os.environ["VIDEO_REFINER_INSTALL_ROOT"]).resolve()
app_home = Path(os.environ["VIDEO_REFINER_INSTALL_APP_HOME"]).expanduser()
config_path = Path(os.environ["VIDEO_REFINER_INSTALL_CONFIG"]).expanduser()
python_bin = Path(os.environ["VIDEO_REFINER_INSTALL_PYTHON"]).resolve()
ffmpeg_bin = os.environ["VIDEO_REFINER_INSTALL_FFMPEG"]

defaults = {
    "database_path": str(app_home / "video-refiner.sqlite3"),
    "output_root": str(Path.home() / "Desktop" / "视频炼化输出"),
    "prompts_dir": str(root / "prompts"),
    "system_python": str(python_bin),
    "camoufox_python": str(python_bin),
    "ffmpeg_bin": ffmpeg_bin,
    "daily_limit": 50,
    "video_delay_min_ms": 3000,
    "video_delay_max_ms": 8000,
    "dimension_delay_min_ms": 10000,
    "dimension_delay_max_ms": 20000,
    "auto_retry_max_attempts": 3,
    "auto_retry_delay_min_ms": 60000,
    "auto_retry_delay_max_ms": 180000,
    "frame_fps": 1,
    "max_dimension_frames": 20,
    "max_analysis_chars_per_video": 60000,
    "max_merge_chars_per_video": 500,
}

if config_path.exists():
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
else:
    data = {}

for key, value in defaults.items():
    data.setdefault(key, value)
for key in ["prompts_dir", "system_python", "camoufox_python", "ffmpeg_bin"]:
    data[key] = defaults[key]

config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
os.chmod(config_path, 0o600)
PY
}

cd "$ROOT_DIR"

log "检查 Python"
HOST_PYTHON="$(find_compatible_python)"
echo "使用 Python：$HOST_PYTHON ($("$HOST_PYTHON" --version 2>&1))"

log "创建后端 Python 虚拟环境"
prepare_venv
"$PYTHON_BIN" -m pip install --upgrade pip setuptools wheel
"$PYTHON_BIN" -m pip install -r "$BACKEND_DIR/requirements.txt"
ensure_whisper_model_cache
install_camoufox_browsers
verify_runtime_imports

log "检查 ffmpeg"
install_ffmpeg_if_needed

log "检查必需下载组件"
install_required_node_tools

log "检查前端构建产物"
build_frontend_if_needed

log "写入本机配置：$CONFIG_PATH"
write_config

echo
echo "安装完成。现在可以双击 start.command 启动。"
