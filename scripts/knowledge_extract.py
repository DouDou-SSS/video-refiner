#!/usr/bin/env python3
"""
知识库提炼脚本 v1.0

功能：从视频中提取知识内容
- 视频下载（支持多平台）
- 文案提取（优先字幕 → 快速OCR → Whisper兜底）
- 每秒1帧抽帧
- LLM多模态知识提炼（文案+帧图互补）

不做什么：
- 不做5维度分析（文案风格、视频脚本等）
- 不分析视频制作手法和播放数据
- 不生成5个产品.md文件
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


# ============================================================
# 配置
# ============================================================

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
WHISPER_SERVICE = os.path.expanduser("~/.openclaw/workspace/skills/video-refiner/scripts/whisper_service.py")
CROSS_VALIDATE = os.path.expanduser("~/.openclaw/workspace/skills/video-refiner/scripts/cross_validate.py")
WHISPER_TRANSCRIBE = os.path.expanduser("~/.openclaw/workspace/skills/video-refiner/scripts/whisper_transcribe.py")
SYSTEM_PYTHON = "/opt/homebrew/bin/python3"
CAMOUFOX_ENV = os.path.expanduser("~/camoufox-env/bin/python3")


def read_openclaw_config():
    """从 ~/.openclaw/openclaw.json 读取模型配置"""
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        # 提取百炼 API Key
        models = config.get('models', {})
        providers = models.get('providers', {})
        bailian = providers.get('bailian', {})
        api_key = bailian.get('apiKey', '')
        if not api_key:
            # 尝试从环境变量获取
            api_key = os.environ.get('DASHSCOPE_API_KEY', '')
        return {
            'api_key': api_key,
            'base_url': 'https://coding.dashscope.aliyuncs.com/v1',
            'model': 'qwen3.6-plus',
        }
    except Exception as e:
        print(f"⚠️ 读取 openclaw.json 失败: {e}")
        return {
            'api_key': os.environ.get('DASHSCOPE_API_KEY', ''),
            'base_url': 'https://coding.dashscope.aliyuncs.com/v1',
            'model': 'qwen3.6-plus',
        }


def extract_video_id(url):
    """从抖音/B站链接提取视频ID"""
    # 抖音
    match = re.search(r'modal_id=(\d+)', url)
    if match:
        return match.group(1), 'douyin'
    match = re.search(r'/video/(\d+)', url)
    if match:
        return match.group(1), 'douyin'
    # B站
    match = re.search(r'(BV[\w]+)', url)
    if match:
        return match.group(1), 'bilibili'
    # 纯数字ID
    if re.match(r'^\d+$', url):
        return url, 'douyin'
    return url, 'unknown'


def download_video(video_id, platform, output_dir):
    """
    下载视频。复用 video-refiner 的 download_router.py。
    """
    scripts_dir = os.path.expanduser("~/.openclaw/workspace/skills/video-refiner/scripts/")
    download_router = os.path.join(scripts_dir, 'download_router.py')

    # 构造完整 URL
    if platform == 'douyin':
        video_url = f'https://www.douyin.com/video/{video_id}'
    elif platform == 'bilibili':
        video_url = f'https://www.bilibili.com/video/{video_id}'
    else:
        # 未知平台，当作纯 ID 尝试
        video_url = video_id

    # 先用 download_router.py 尝试智能下载
    if os.path.exists(download_router):
        try:
            result = subprocess.run(
                [SYSTEM_PYTHON, download_router, video_url, '--output', os.path.join(output_dir, f'{video_id}.mp4')],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if not data.get('error'):
                    video_path = os.path.join(output_dir, f'{video_id}.mp4')
                    if os.path.exists(video_path) and os.path.getsize(video_path) > 1000:
                        size_mb = os.path.getsize(video_path) / (1024 * 1024)
                        print(f"✅ 智能下载成功 ({size_mb:.1f}MB, method={data.get('method', 'unknown')})")
                        return True
        except Exception as e:
            print(f"⚠️ 智能下载失败: {e}")

    # 回退：Camoufox 获取
    get_video_info = os.path.join(scripts_dir, 'get_video_info.py')
    if os.path.exists(get_video_info):
        try:
            result = subprocess.run(
                [CAMOUFOX_ENV, get_video_info, video_id],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                info = json.loads(result.stdout.strip())
                if isinstance(info, list) and len(info) > 0:
                    info = info[0]
                cdn_url = info.get('playUrl', '')
                if cdn_url:
                    video_path = os.path.join(output_dir, f"{video_id}.mp4")
                    subprocess.run(
                        ['curl', '-L', '-o', video_path, cdn_url],
                        capture_output=True, timeout=300
                    )
                    if os.path.exists(video_path) and os.path.getsize(video_path) > 1000:
                        print(f"✅ Camoufox 下载成功: {video_path}")
                        return True
        except Exception as e:
            print(f"⚠️ Camoufox 下载失败: {e}")

    print(f"❌ 所有下载方式均失败")
    return False


def extract_frames(video_path, frames_dir, fps=1):
    """用 ffmpeg 抽帧，每秒 fps 帧"""
    os.makedirs(frames_dir, exist_ok=True)
    
    cmd = [
        'ffmpeg', '-i', video_path,
        '-vf', f'fps={fps}',
        '-q:v', '2',
        os.path.join(frames_dir, 'frame_%04d.jpg')
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    frame_count = len([f for f in os.listdir(frames_dir) if f.startswith('frame_') and f.endswith('.jpg')])
    print(f"✅ 抽帧完成: {frame_count} 张帧图 ({fps}帧/秒)")
    return frame_count


def extract_transcript(video_id, video_path, output_dir, raw_data_dir, frames_dir, api_key, title=''):
    """
    提取文案（v7 流程：优先字幕 → OCR → Whisper兜底 → FunASR标点恢复）

    不依赖 cross_validate.py（该脚本期望预跑的 Whisper 文件，不适合独立运行场景）。
    直接实现完整的字幕/OCR/Whisper 提取 + FunASR 标点恢复。
    """
    os.makedirs(raw_data_dir, exist_ok=True)
    os.makedirs(frames_dir, exist_ok=True)

    # --- 步骤1: 尝试 OCR 硬字幕 ---
    print(f"  🔍 检测硬字幕 OCR（1秒1帧，RapidOCR 本地引擎）...")
    ocr_text = ''
    try:
        from rapidocr_onnxruntime import RapidOCR
        ocr_engine = RapidOCR()

        frames = sorted([f for f in os.listdir(frames_dir) if f.endswith('.jpg')])
        if frames:
            all_texts = []
            for frame_name in frames:
                frame_path = os.path.join(frames_dir, frame_name)
                result, _ = ocr_engine(frame_path)
                if result:
                    all_texts.extend([line[1] for line in result])

            ocr_text = ' '.join(all_texts)
            if ocr_text.strip():
                # 保存 OCR 结果
                with open(os.path.join(raw_data_dir, 'ocr_results.txt'), 'w', encoding='utf-8') as f:
                    f.write(ocr_text)
                print(f"  ✅ OCR 提取到 {len(ocr_text)} 字")
            else:
                print(f"  ⚠️ 未检测到硬字幕")
    except ImportError:
        print(f"  ⚠️ RapidOCR 未安装，跳过 OCR")

    # --- 步骤2: 无字幕时使用 Whisper ---
    whisper_text = ''
    if not ocr_text.strip():
        print(f"  🎙️ 无硬字幕，使用 Whisper 语音识别...")
        try:
            from faster_whisper import WhisperModel
            model_size = 'medium'
            model_path = os.path.expanduser(
                f'~/.cache/huggingface/hub/models--Systran--faster-whisper-{model_size}')
            if not os.path.exists(model_path):
                model_size = 'base'

            model = WhisperModel(model_size, device='cpu', compute_type='int8')
            segments, _ = model.transcribe(video_path, beam_size=5, language='zh')
            whisper_text = ' '.join([s.text for s in segments])
            if whisper_text.strip():
                print(f"  ✅ Whisper 提取到 {len(whisper_text)} 字")
        except Exception as e:
            print(f"  ⚠️ Whisper 失败: {e}")
    else:
        print(f"  ✅ 使用 OCR 字幕，跳过 Whisper")

    # 确定最终文本
    main_text = ocr_text.strip() if ocr_text.strip() else whisper_text.strip()
    if not main_text:
        print(f"  ❌ 无任何文案来源")
        return None

    # --- 步骤3: FunASR 标点恢复 ---
    print(f"  ✏️ FunASR 添加标点 + 分段...")
    try:
        from funasr import AutoModel
        model_path = os.environ.get(
            'FUNASR_PUNC_MODEL_PATH',
            os.path.expanduser('~/.cache/modelscope/hub/models/damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch'))
        if os.path.exists(model_path) or os.path.exists(os.path.expanduser('~/.cache/modelscope/hub/models/damo')):
            model = AutoModel(model=model_path)
            result = model.generate(input=main_text)
            corrected = result[0]['text'] if isinstance(result, list) else result.get('text', '')
            if corrected:
                corrected_text = corrected
            else:
                corrected_text = main_text
        else:
            print(f"  ⚠️ FunASR 模型未下载，跳过标点恢复")
            corrected_text = main_text
    except Exception as e:
        print(f"  ⚠️ FunASR 失败: {e}，使用原始文本")
        corrected_text = main_text

    # 保存文案
    transcript_file = os.path.join(output_dir, 'transcript.md')
    with open(transcript_file, 'w', encoding='utf-8') as f:
        f.write(f"# 视频文案\n\n> 视频ID: {video_id}\n> 来源: {'OCR' if ocr_text.strip() else 'Whisper'} + FunASR标点\n\n---\n\n{corrected_text}\n")
    print(f"  ✅ 文案已保存: transcript.md ({len(corrected_text)} 字)")

    return corrected_text


def extract_knowledge(transcript, frames_dir, frame_count, video_info, config):
    """
    使用 LLM 多模态提炼知识
    文案 + 帧图 → 完整知识提炼文档
    """
    print(f"\n📝 开始知识提炼...")
    
    try:
        from openai import OpenAI
    except ImportError:
        subprocess.run([SYSTEM_PYTHON, '-m', 'pip', 'install', 'openai'], capture_output=True)
        from openai import OpenAI
    
    client = OpenAI(
        api_key=config['api_key'],
        base_url=config['base_url'],
    )
    
    # 构建多模态消息
    content_parts = []
    
    # 系统提示
    system_prompt = """你是一个知识提炼专家。请仔细分析视频的文案和画面内容，将视频中讲到的知识完整、系统地提炼出来。

要求：
1. 按主题/章节分段整理
2. 保留关键信息、数据、步骤、案例
3. 文案和画面互补：文案讲的内容 + 画面展示的内容都要纳入
4. 去除水词、重复、无意义的过渡语
5. 保留专业术语和核心概念
6. 输出格式清晰，使用 Markdown

输出格式：
# [视频标题] 知识提炼

## 📋 概览
（一句话总结视频讲了什么）

## 📚 知识点详情

### 一、[主题1]
- 详细内容...
- 🖼️ 画面补充：（对应时间点画面展示了什么图表、演示、文字等）

### 二、[主题2]
...

## 🔑 关键要点总结
- 核心要点1
- 核心要点2

## 📎 参考资料
- 时间点标注（方便回看原视频）"""
    
    content_parts.append({"type": "text", "text": system_prompt})
    
    # 加入文案
    if transcript:
        # 如果文案太长，截取前80000字符
        truncated = transcript[:80000] if len(transcript) > 80000 else transcript
        content_parts.append({
            "type": "text",
            "text": f"\n\n【视频文案】\n{truncated}"
        })
    
    # 加入帧图（最多40张，分散选取）
    if os.path.exists(frames_dir) and frame_count > 0:
        frame_files = sorted([f for f in os.listdir(frames_dir) if f.endswith('.jpg')])
        
        # 均匀选取，最多40张
        max_frames = 40
        if len(frame_files) > max_frames:
            step = len(frame_files) // max_frames
            selected = [frame_files[i] for i in range(0, len(frame_files), step)][:max_frames]
        else:
            selected = frame_files
        
        print(f"   选取 {len(selected)} 张帧图用于知识提炼")
        
        import base64
        for frame_file in selected:
            frame_path = os.path.join(frames_dir, frame_file)
            with open(frame_path, 'rb') as f:
                img_base64 = base64.b64encode(f.read()).decode()
            content_parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{img_base64}"
                }
            })
        
        # 帧图说明
        content_parts.append({
            "type": "text",
            "text": f"\n\n以上是从视频中抽取的 {len(selected)} 张帧图（每秒1帧，共 {frame_count} 帧，均匀选取）。请结合文案和帧图内容，完整提炼视频中的知识。"
        })
    
    # 视频信息
    if video_info:
        content_parts.append({
            "type": "text",
            "text": f"\n\n【视频信息】\n{json.dumps(video_info, ensure_ascii=False, indent=2)}"
        })
    
    print(f"   调用 LLM ({config['model']})...")
    
    response = client.chat.completions.create(
        model=config['model'],
        messages=[{"role": "user", "content": content_parts}],
        max_tokens=8000,
        temperature=0.3,
    )
    
    knowledge = response.choices[0].message.content
    print(f"✅ 知识提炼完成 ({len(knowledge)} 字符)")
    return knowledge


def classify_category(knowledge, title, config):
    """用 LLM 判断知识提炼文档的分类目录名"""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=config['api_key'], base_url=config['base_url'])

        # 提取分类规则：已有分类 + 新分类判断（路径可通过环境变量 VIDEO_REFINER_OBSIDIAN_VAULT 覆盖）
        obsidian_vault = Path(os.environ.get('VIDEO_REFINER_OBSIDIAN_VAULT', os.path.expanduser('~/Obsidian-Vault/知识库')))
        existing_dirs = [d.name for d in obsidian_vault.iterdir() if d.is_dir() and not d.name.startswith('.')]

        prompt = f"""你是一个知识分类专家。请根据以下内容判断分类目录名。

已有分类目录：{', '.join(existing_dirs) if existing_dirs else '（暂无）'}

视频标题：{title}

知识提炼摘要（前1500字）：
{knowledge[:1500]}

要求：
1. 如果内容适合已有分类，直接返回分类目录名（必须完全一致）
2. 如果不适合任何已有分类，创建新的分类目录名（用中文，简洁）
3. 只返回分类目录名，不要其他内容

返回格式示例：Claude Code
"""

        resp = client.chat.completions.create(
            model=config['model'],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0.1,
        )
        category = resp.choices[0].message.content.strip().replace('/', '-').replace('\\', '-')

        # 简单清理：去掉引号、括号等
        category = category.strip('"\'`')
        if len(category) > 30:
            category = category[:30]

        return category

    except Exception as e:
        print(f"⚠️ 分类失败: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description='知识库提炼脚本')
    parser.add_argument('url_or_id', help='视频链接或视频ID')
    parser.add_argument('--title', help='视频标题（可选）')
    parser.add_argument('--output-dir', help='自定义输出目录')
    args = parser.parse_args()
    
    video_id, platform = extract_video_id(args.url_or_id)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # 输出目录
    if args.output_dir:
        output_base = Path(args.output_dir)
    else:
        output_base = BASE_DIR / f"knowledge_{video_id}_{timestamp}"
    
    output_base.mkdir(parents=True, exist_ok=True)
    
    # 子目录
    frames_dir = output_base / 'frames'
    raw_data_dir = output_base / 'raw_data'
    frames_dir.mkdir(exist_ok=True)
    raw_data_dir.mkdir(exist_ok=True)
    
    print(f"🎬 知识库提炼开始")
    print(f"   视频ID: {video_id}")
    print(f"   平台: {platform}")
    print(f"   输出: {output_base}")
    
    # 读取配置
    config = read_openclaw_config()
    if not config['api_key']:
        print("❌ 未找到 API Key，请检查 ~/.openclaw/openclaw.json 或设置 DASHSCOPE_API_KEY")
        sys.exit(1)

    video_title = args.title or ''
    
    # 1. 下载视频
    print(f"\n⬇️  步骤1: 下载视频")
    video_path = output_base / f"{video_id}.mp4"
    
    # 如果视频已存在，跳过下载
    if video_path.exists() and os.path.getsize(video_path) > 1000:
        print(f"✅ 视频已存在: {video_path}")
    else:
        success = download_video(video_id, platform, str(output_base))
        if not success:
            print("❌ 视频下载失败")
            sys.exit(1)
    
    video_path = str(video_path)
    
    # 2. 抽帧
    print(f"\n🖼️  步骤2: 视频抽帧 (1帧/秒)")
    frame_count = extract_frames(video_path, str(frames_dir), fps=1)
    
    # 3. 提取文案
    print(f"\n📝 步骤3: 提取文案 (v7: 优先字幕 → OCR → Whisper)")
    transcript = extract_transcript(video_id, video_path, str(output_base), str(raw_data_dir), str(frames_dir), config['api_key'], video_title)
    
    if transcript:
        with open(output_base / 'transcript.md', 'w') as f:
            f.write(transcript)
        print(f"✅ 文案已保存: transcript.md")
    
    if not transcript and frame_count == 0:
        print("❌ 文案和帧图都未获取，无法继续")
        sys.exit(1)
    
    # 4. 知识提炼
    print(f"\n🧠 步骤4: LLM 知识提炼")
    video_info = {
        'video_id': video_id,
        'platform': platform,
        'title': video_title,
        'frame_count': frame_count,
        'timestamp': timestamp,
    }
    
    knowledge = extract_knowledge(transcript, str(frames_dir), frame_count, video_info, config)

    if not knowledge:
        print("❌ 知识提炼失败")
        sys.exit(1)

    # 5. 分类判断 + 写入 Obsidian
    print(f"\n📂 步骤5: 写入 Obsidian 知识库")
    obsidian_vault = Path(os.environ.get('VIDEO_REFINER_OBSIDIAN_VAULT', os.path.expanduser('~/Obsidian-Vault/知识库')))
    category = classify_category(knowledge, video_title, config)

    if category:
        target_dir = obsidian_vault / category
        target_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{video_title[:50].strip()}.md"
        target_file = target_dir / file_name
        with open(target_file, 'w', encoding='utf-8') as f:
            f.write(knowledge)
        print(f"✅ 已写入 Obsidian: {target_file}")
        print(f"📂 分类: {category}")
    else:
        print("⚠️ 分类判断失败，仅保存到原始输出目录")

    # 同时保留本地原始文件
    knowledge_file = output_base / '知识提炼.md'
    with open(knowledge_file, 'w', encoding='utf-8') as f:
        f.write(knowledge)
    print(f"📄 本地副本: {knowledge_file}")

    print(f"\n📂 知识库目录: {output_base}")
    print("   ├── frames/          ← 抽帧图片")
    print("   ├── 知识提炼.md       ← 最终知识文档")
    print("   ├── transcript.md    ← 完整文案")
    print(f"   └── {video_id}.mp4  ← 原始视频")


if __name__ == '__main__':
    main()
