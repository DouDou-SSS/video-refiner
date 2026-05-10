#!/usr/bin/env python3
"""
常驻 Whisper 服务 — 一次加载模型，串流转处理多个视频
通过 stdin 接收指令，通过 stdout 返回结果

输入格式（每行一条）：
  TRANSCRIBE <video_path> <video_id> <model_size>
  QUIT

输出格式：
  RESULT_OK <video_id> <transcript_text_length>
  <transcript_text>
  RESULT_EOF
  或
  RESULT_ERR <video_id> <error_message>
  RESULT_EOF
"""

import sys
import os
import json

os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'

from faster_whisper import WhisperModel

def log(msg):
    """输出日志到 stderr，不干扰 stdout 数据通道"""
    print(msg, file=sys.stderr, flush=True)

def load_model(model_size):
    log(f"🔄 加载 Whisper 模型: {model_size} ...")
    # Mac mini Apple Silicon: 强制用 int8 量化，速度比 auto 快
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    log(f"✅ 模型 {model_size} 加载完成 (compute_type=int8, Apple Silicon)")
    return model

def transcribe(model, video_path):
    segments, info = model.transcribe(
        video_path,
        beam_size=5,
        language="zh"
    )
    text_parts = []
    for segment in segments:
        text_parts.append(segment.text)
    return " ".join(text_parts).strip()

def main():
    log("=== Whisper 常驻服务启动 ===")
    log("等待指令...")
    # 向 stdout 发送就绪信号
    print("WHISPER_READY", flush=True)

    current_model = None
    current_model_size = None

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            if line == "QUIT":
                log("收到 QUIT 指令，退出服务")
                break

            if line.startswith("TRANSCRIBE "):
                parts = line.split(" ", 3)
                if len(parts) < 4:
                    print("RESULT_ERR UNKNOWN 参数不足: " + line, flush=True)
                    print("RESULT_EOF", flush=True)
                    continue

                video_path = parts[1]
                video_id = parts[2]
                model_size = parts[3]

                # 检查模型是否需要切换
                if current_model is None or current_model_size != model_size:
                    current_model = load_model(model_size)
                    current_model_size = model_size

                log(f"🎙️ 转录: {video_id} (model={model_size})")
                log(f"   路径: {video_path}")

                try:
                    text = transcribe(current_model, video_path)
                    if text:
                        print(f"RESULT_OK {video_id} {len(text)}", flush=True)
                        print(text, flush=True)
                    else:
                        print(f"RESULT_ERR {video_id} 转录结果为空", flush=True)
                    print("RESULT_EOF", flush=True)
                    log(f"✅ 完成: {video_id} ({len(text)}字)")
                except Exception as e:
                    print(f"RESULT_ERR {video_id} {str(e)}", flush=True)
                    print("RESULT_EOF", flush=True)
                    log(f"❌ 失败: {video_id} - {e}")
            else:
                log(f"⚠️ 未知指令: {line}")
                print("RESULT_ERR UNKNOWN 未知指令: " + line, flush=True)
                print("RESULT_EOF", flush=True)

    except KeyboardInterrupt:
        log("服务被中断")
    except Exception as e:
        log(f"服务异常: {e}")
    finally:
        log("Whisper 服务已退出")

if __name__ == "__main__":
    main()
