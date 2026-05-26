#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_HOME="${VIDEO_REFINER_HOME:-$HOME/.video-refiner}"
BACKEND_DIR="$ROOT_DIR/webapp/backend"
NODE_TOOLS_DIR="$ROOT_DIR/webapp/node-tools"
PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
CONFIG_PATH="${VIDEO_REFINER_CONFIG:-$APP_HOME/config.yaml}"
WHISPER_MODEL_DIR="$ROOT_DIR/models/whisper/faster-whisper-tiny"
URL="http://127.0.0.1:7860"

pause_on_error() {
  local code=$?
  if [[ $code -ne 0 && -t 0 ]]; then
    echo
    echo "启动失败，退出码：$code。请查看上面的错误信息，按回车关闭窗口。"
    read -r
  fi
  exit $code
}
trap pause_on_error EXIT

cd "$ROOT_DIR"

venv_python_ok() {
  [[ -x "$PYTHON_BIN" ]] || return 1
  "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import sys
major, minor = sys.version_info[:2]
if major != 3 or minor < 10 or minor > 12:
    raise SystemExit(1)
PY
}

runtime_ready() {
  venv_python_ok || return 1
  [[ -f "$ROOT_DIR/webapp/frontend/dist/index.html" ]] || return 1
  [[ -x "$NODE_TOOLS_DIR/node_modules/.bin/mcporter" ]] || return 1
  [[ -x "$NODE_TOOLS_DIR/node_modules/.bin/opencli" ]] || return 1
  [[ -f "$WHISPER_MODEL_DIR/model.bin" ]] || return 1
  [[ -f "$WHISPER_MODEL_DIR/config.json" ]] || return 1
  [[ -f "$WHISPER_MODEL_DIR/tokenizer.json" ]] || return 1
  "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import importlib.util
required = ["fastapi", "uvicorn", "openai", "yaml", "faster_whisper", "rapidocr_onnxruntime", "yt_dlp", "torch", "torchaudio", "funasr", "camoufox"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(1)
PY
}

if [[ -x "$PYTHON_BIN" ]] && ! venv_python_ok; then
  echo "检测到虚拟环境 Python 版本不兼容，重新安装依赖。"
  rm -rf "$BACKEND_DIR/.venv"
fi

if ! runtime_ready; then
  echo "检测到尚未安装依赖或缺少前端构建产物，先运行 install.command。"
  VIDEO_REFINER_NO_PAUSE=1 "$ROOT_DIR/install.command"
fi

export PATH="$BACKEND_DIR/.venv/bin:$NODE_TOOLS_DIR/node_modules/.bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONPATH="$BACKEND_DIR"
export VIDEO_REFINER_CONFIG="$CONFIG_PATH"
export VIDEO_REFINER_PROMPTS_DIR="$ROOT_DIR/prompts"
export VIDEO_REFINER_SYSTEM_PYTHON="$PYTHON_BIN"
export VIDEO_REFINER_CAMOUFOX_PYTHON="$PYTHON_BIN"
export VIDEO_REFINER_WHISPER_MODEL_PATH="$WHISPER_MODEL_DIR"
export VIDEO_REFINER_WHISPER_LOCAL_ONLY=1
export HF_HOME="$ROOT_DIR/models/huggingface"
export HUGGINGFACE_HUB_CACHE="$ROOT_DIR/models/huggingface/hub"

HEALTH_JSON="$(curl -fsS "$URL/api/health" 2>/dev/null || true)"
if [[ -n "$HEALTH_JSON" ]]; then
  if VIDEO_REFINER_EXPECTED_ROOT="$ROOT_DIR" VIDEO_REFINER_HEALTH_JSON="$HEALTH_JSON" "$PYTHON_BIN" -c '
import json
import os

health = json.loads(os.environ["VIDEO_REFINER_HEALTH_JSON"])
expected_root = os.path.realpath(os.environ["VIDEO_REFINER_EXPECTED_ROOT"])
actual_root = os.path.realpath(health.get("repo_root", ""))
if actual_root != expected_root or not health.get("bundled_whisper_model"):
    raise SystemExit(1)
' >/dev/null 2>&1
  then
    echo "视频炼化已经在运行：$URL"
    open "$URL"
    exit 0
  fi

  echo "检测到 7860 端口上已有旧版或其他视频炼化服务。"
  echo "请先关闭旧的 start.command 终端窗口，或结束下面的占用进程后再启动新版："
  lsof -nP -iTCP:7860 -sTCP:LISTEN || true
  exit 1
fi

if lsof -tiTCP:7860 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 7860 已被其他程序占用，请关闭占用程序后重试。"
  lsof -nP -iTCP:7860 -sTCP:LISTEN
  exit 1
fi

echo "启动视频炼化：$URL"
echo "关闭本窗口会停止软件。"
(sleep 2; open "$URL") &

exec "$PYTHON_BIN" -m uvicorn videorefiner_app.main:app --host 127.0.0.1 --port 7860
