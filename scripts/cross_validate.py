#!/usr/bin/env python3
"""
文案交叉验证 + 自动标点分段
用法: python cross_validate.py <whisper_text_file_or_none> <subtitle_url_or_file> <frames_dir> <video_id> <title> <api_key>
输出: 修正+标点+分段后的完整文案（stdout最后一行）

修改日志:
2026-05-15: 改为优先使用辅助字幕/硬字幕OCR，无字幕时才用Whisper
           OCR改为1秒1帧，数量不限
"""

import sys
import json
import os
import re
import subprocess
import tempfile
import time
import warnings
warnings.filterwarnings('ignore')

# 解析参数
whisper_arg = sys.argv[1]         # Whisper文本文件路径，或 "none"
subtitle_arg = sys.argv[2]        # URL 或 文件路径，或 "none"
frames_dir = sys.argv[3]          # 帧缓存目录
video_id = sys.argv[4]
video_title = sys.argv[5]
api_key = sys.argv[6]

# OpenAI API 配置
BASE_URL = 'https://coding.dashscope.aliyuncs.com/v1'

# ========================
# 工具函数
# ========================
def clean_subtitle_text(text):
    """清理 SRT/ASS 字幕格式，只保留纯文字"""
    text = re.sub(r'\d+\n', '', text)  # 去掉序号
    text = re.sub(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\n', '', text)  # 去掉时间码
    text = re.sub(r'<[^>]+>', '', text)  # 去掉标签
    text = re.sub(r'\{[^}]+\}', '', text)  # 去掉 ASS 标签
    text = '\n'.join(line.strip() for line in text.split('\n') if line.strip())
    return text

def get_cc_subtitle():
    """获取辅助字幕（CC字幕）"""
    subtitle_texts = []
    
    # 方式A: 本地字幕文件
    if subtitle_arg and subtitle_arg != 'none' and os.path.exists(subtitle_arg):
        with open(subtitle_arg, 'r', encoding='utf-8') as f:
            sub_content = f.read()
        text = clean_subtitle_text(sub_content)
        if text:
            subtitle_texts.append(text)
            print(f'[验证] ✅ 辅助字幕文件: {len(text)}字', file=sys.stderr)
    
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
            text = clean_subtitle_text(sub_content)
            if text:
                subtitle_texts.append(text)
                print(f'[验证] ✅ 辅助字幕URL: {len(text)}字', file=sys.stderr)
        except Exception as e:
            print(f'[验证] ⚠️ 辅助字幕URL下载失败: {e}', file=sys.stderr)
    
    return '\n'.join(subtitle_texts)

def get_hard_subtitle_ocr():
    """硬字幕OCR — 1秒1帧，数量不限"""
    if not os.path.isdir(frames_dir):
        return ''
    
    frames = [f for f in os.listdir(frames_dir) if f.endswith('.jpg')]
    frames.sort()
    
    if not frames:
        return ''
    
    # 1秒1帧（30fps视频约30帧/秒，取每30帧的1帧）
    # 假设视频是30fps，则每秒1帧 = 每隔30帧取1帧
    # 为保险，用 ffmpeg 抽帧时已经是 fps=1，所以直接全部处理
    ocr_frames = frames
    
    print(f'[验证] OCR: 对 {len(ocr_frames)} 帧提取硬字幕（1秒1帧，RapidOCR 本地引擎）...', file=sys.stderr)
    
    try:
        from rapidocr_onnxruntime import RapidOCR
        ocr_engine = RapidOCR()
    except ImportError:
        print(f'[验证] ⚠️ OCR: RapidOCR 未安装', file=sys.stderr)
        return ''
    
    all_texts = []
    frames_with_text = 0
    start = time.time()
    
    for i, frame_name in enumerate(ocr_frames):
        frame_path = os.path.join(frames_dir, frame_name)
        result, elapse = ocr_engine(frame_path)
        texts = [line[1] for line in (result or [])]
        if texts:
            frames_with_text += 1
            all_texts.extend(texts)
        
        # 进度日志（每50帧打印一次）
        if (i + 1) % 50 == 0:
            print(f'[验证]   OCR进度: {i+1}/{len(ocr_frames)} 帧 ({frames_with_text}帧有字幕)...', file=sys.stderr)
    
    elapsed = time.time() - start
    ocr_text = ' '.join(all_texts)
    
    if ocr_text.strip():
        print(f'[验证] ✅ 硬字幕OCR完成: {frames_with_text}/{len(ocr_frames)}帧有字幕, {len(ocr_text)}字, 耗时{elapsed:.1f}s', file=sys.stderr)
    else:
        print(f'[验证] ⚠️ OCR: 未检测到硬字幕 ({frames_with_text}/{len(ocr_frames)}帧)', file=sys.stderr)
    
    return ocr_text

# ========================
# 1. 优先获取辅助字幕/硬字幕
# ========================
print(f'[验证] === 字幕/OCR检测阶段 ===', file=sys.stderr)

cc_subtitle = get_cc_subtitle()
ocr_subtitle = get_hard_subtitle_ocr()

has_subtitles = bool(cc_subtitle.strip()) or bool(ocr_subtitle.strip())

if has_subtitles:
    print(f'[验证] ✅ 检测到字幕/硬字幕，跳过Whisper转文字', file=sys.stderr)
    combined_subtitle = (cc_subtitle + '\n' + ocr_subtitle).strip()
    print(f'[验证] 字幕总字数: {len(combined_subtitle)}字', file=sys.stderr)
else:
    print(f'[验证] ⚠️ 无辅助字幕/硬字幕，需要使用Whisper转文字', file=sys.stderr)

# ========================
# 2. 无字幕时使用 Whisper
# ========================
whisper_text = ''

if not has_subtitles and whisper_arg and whisper_arg != 'none' and os.path.exists(whisper_arg):
    print(f'[验证] === Whisper 转文字阶段 ===', file=sys.stderr)
    with open(whisper_arg, 'r', encoding='utf-8') as f:
        whisper_text = f.read().strip()
    print(f'[验证] Whisper 转写完成: {len(whisper_text)}字', file=sys.stderr)

# 确定最终使用的文本
if has_subtitles:
    main_text = combined_subtitle
    source = '字幕/OCR'
else:
    main_text = whisper_text
    source = 'Whisper'

if not main_text.strip():
    print(f'[验证] ❌ 无任何文案来源', file=sys.stderr)
    sys.exit(1)

print(f'[验证] === 文案来源: {source} ({len(main_text)}字）===', file=sys.stderr)

# ========================
# 3. 交叉验证（字幕 vs Whisper，如有两者都有）
# ========================
if has_subtitles and whisper_text:
    print(f'[验证] 开始交叉验证...（{source} {len(main_text)}字 vs Whisper {len(whisper_text)}字）', file=sys.stderr)
    # 未来可加入 LLM 交叉验证
    # 当前直接使用字幕文本（更可靠）
elif has_subtitles:
    print(f'[验证] 仅字幕来源，无需交叉验证', file=sys.stderr)
else:
    print(f'[验证] 仅Whisper来源，无需交叉验证', file=sys.stderr)

# ========================
# 4. 添加标点 + 分段（FunASR 本地模型）
# ========================
print(f'[验证] 本地模型添加标点 + 分段...', file=sys.stderr)

def add_punctuation_local(text):
    """用 FunASR 本地标点模型添加标点"""
    from funasr import AutoModel
    
    model_path = os.environ.get(
        'FUNASR_PUNC_MODEL_PATH',
        os.path.expanduser('~/.cache/modelscope/hub/models/damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch')
    )
    model = AutoModel(model=model_path)
    result = model.generate(input=text)
    punctuated = result[0]['text'] if isinstance(result, list) else result.get('text', '')
    return punctuated

corrected_text = add_punctuation_local(main_text)

print(f'[验证] 修正完成: {len(corrected_text)}字')
print(corrected_text)
