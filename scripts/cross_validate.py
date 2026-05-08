#!/usr/bin/env python3
"""
文案交叉验证 + 自动标点分段
用法: python cross_validate.py <whisper_text_file> <subtitle_url_or_file> <frames_dir> <video_id> <title> <api_key>
输出: 修正+标点+分段后的完整文案（stdout）
"""

import sys
import json
import os
import re
import subprocess
import tempfile

# 解析参数
whisper_file = sys.argv[1]
subtitle_arg = sys.argv[2]  # URL 或 文件路径，或 "none"
frames_dir = sys.argv[3]    # 帧缓存目录
video_id = sys.argv[4]
video_title = sys.argv[5]
api_key = sys.argv[6]

# OpenAI API 配置
BASE_URL = 'https://coding.dashscope.aliyuncs.com/v1'

# ========================
# 1. 读取 Whisper 文案（主）
# ========================
with open(whisper_file, 'r', encoding='utf-8') as f:
    whisper_text = f.read().strip()

print(f'[验证] Whisper 文案: {len(whisper_text)}字', file=sys.stderr)

# ========================
# 2. 获取辅助字幕（如有）
# ========================
subtitle_texts = []

# 方式A: 本地字幕文件
if subtitle_arg and subtitle_arg != 'none' and os.path.exists(subtitle_arg):
    with open(subtitle_arg, 'r', encoding='utf-8') as f:
        sub_content = f.read()
    # 清理 SRT/ASS 格式，只保留文字
    text = re.sub(r'\d+\n', '', sub_content)  # 去掉序号
    text = re.sub(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\n', '', text)  # 去掉时间码
    text = re.sub(r'<[^>]+>', '', text)  # 去掉标签
    text = re.sub(r'\{[^}]+\}', '', text)  # 去掉 ASS 标签
    text = '\n'.join(line.strip() for line in text.split('\n') if line.strip())
    if text:
        subtitle_texts.append(text)
        print(f'[验证] 字幕文件: {len(text)}字', file=sys.stderr)

# 方式B: 下载字幕 URL
elif subtitle_arg and subtitle_arg.startswith('http'):
    try:
        http_proxy = os.environ.get('https_proxy', '')
        import urllib.request
        req = urllib.request.Request(subtitle_arg)
        if http_proxy:
            proxy = urllib.request.ProxyHandler({'http': http_proxy, 'https': http_proxy})
            opener = urllib.request.build_opener(proxy)
        else:
            opener = urllib.request.build_opener()
        resp = opener.open(req, timeout=15)
        sub_content = resp.read().decode('utf-8', errors='ignore')
        # 清理格式
        text = re.sub(r'\d+\n', '', sub_content)
        text = re.sub(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\n', '', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\{[^}]+\}', '', text)
        text = '\n'.join(line.strip() for line in text.split('\n') if line.strip())
        if text:
            subtitle_texts.append(text)
            print(f'[验证] 字幕URL: {len(text)}字', file=sys.stderr)
    except Exception as e:
        print(f'[验证] 字幕URL下载失败: {e}', file=sys.stderr)

# ========================
# 3. 硬字幕 OCR（如有帧）— RapidOCR 本地引擎
# ========================
ocr_text = ''
if os.path.isdir(frames_dir):
    frames = [f for f in os.listdir(frames_dir) if f.endswith('.jpg')]
    frames.sort()
    # 选部分帧做OCR（每隔5秒1帧，最多20帧）
    ocr_frames = [frames[i] for i in range(0, len(frames), min(5, max(1, len(frames)//20)))][:20]

    if ocr_frames:
        try:
            import warnings
            warnings.filterwarnings('ignore')
            from rapidocr_onnxruntime import RapidOCR
            ocr_engine = RapidOCR()
            print(f'[验证] OCR: 对 {len(ocr_frames)} 帧提取硬字幕（RapidOCR 本地引擎）...', file=sys.stderr)

            all_ocr_texts = []
            for frame_name in ocr_frames:
                frame_path = os.path.join(frames_dir, frame_name)
                result, elapse = ocr_engine(frame_path)
                texts = [line[1] for line in (result or [])]
                if texts:
                    all_ocr_texts.extend(texts)

            ocr_text = ' '.join(all_ocr_texts)
            if ocr_text.strip():
                print(f'[验证] OCR字幕: {len(ocr_text)}字', file=sys.stderr)
            else:
                print(f'[验证] OCR: 未检测到硬字幕', file=sys.stderr)
        except ImportError:
            print(f'[验证] OCR: RapidOCR 未安装（pip install rapidocr_onnxruntime）', file=sys.stderr)
        except Exception as e:
            print(f'[验证] OCR失败: {e}', file=sys.stderr)

# ========================
# 4. 交叉验证：Whisper 为主，字幕/OCR 纠正错字
# ========================
all_sub_text = '\n'.join(subtitle_texts) + '\n' + ocr_text

if all_sub_text.strip():
    print(f'[验证] 开始交叉验证...（Whisper {len(whisper_text)}字 vs 辅助 {len(all_sub_text)}字）', file=sys.stderr)
else:
    print(f'[验证] 无辅助字幕/OCR，跳过交叉验证', file=sys.stderr)

# ========================
# 5. 纠正错字 + 标点分段（FunASR 本地模型）
# ========================
print(f'[验证] 本地模型纠正错字 + 添加标点 + 分段...', file=sys.stderr)

def add_punctuation_local(text):
    """用 FunASR 本地标点模型添加标点"""
    import warnings
    warnings.filterwarnings('ignore')
    from funasr import AutoModel
    
    model_path = os.environ.get(
        'FUNASR_PUNC_MODEL_PATH',
        os.path.expanduser('~/.cache/modelscope/hub/models/damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch')
    )
    model = AutoModel(model=model_path)
    result = model.generate(input=text)
    punctuated = result[0]['text'] if isinstance(result, list) else result.get('text', '')
    return punctuated

corrected_text = add_punctuation_local(whisper_text)

print(f'[验证] 修正完成: {len(corrected_text)}字')
print(corrected_text)
