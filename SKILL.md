---
name: video-refiner
description: 视频创作者方法论蒸馏工具。通过反检测浏览器获取视频，Whisper 语音识别 + FunASR 标点恢复提取文案，RapidOCR 本地 OCR 提取硬字幕，结合多模态大模型进行多维度创作方法论蒸馏分析，最终输出结构化的 .md 文件。
---

# 🎬 视频炼化

从视频创作者的内容中提取隐性经验，蒸馏为可复用的创作方法论。

## 核心功能

- **反检测下载** — 使用 Camoufox 反检测浏览器获取视频链接，防封控
- **语音转文字** — Whisper large-v3/medium 语音识别，自动根据视频时长切换模型
- **文案纠正** — FunASR CT-Transformer 本地模型自动添加标点符号和分段
- **硬字幕 OCR** — RapidOCR 本地引擎提取画面中的硬字幕，辅助交叉验证
- **多维度蒸馏** — 5 个专用 Prompt 分别分析文案风格、视频脚本、剪辑逻辑、选题策略、运营策略
- **合并精炼输出** — 使用最强 LLM + 思考模式，合并所有视频分析为精炼的 .md 文件

## 快速开始

### 前置依赖

| 依赖 | 用途 | 安装方式 |
|------|------|---------|
| ffmpeg | 视频抽帧 | `brew install ffmpeg` |
| camoufox | 反检测浏览器 | `pip install camoufox` + `python -m camoufox fetch` |
| faster-whisper | 语音转文字 | `pip install faster-whisper` |
| funasr | 标点恢复 | `pip install funasr` |
| rapidocr_onnxruntime | 硬字幕 OCR | `pip install rapidocr_onnxruntime` |

### 运行

```bash
node scripts/analyze_blogger.mjs <视频ID或链接> [--output <输出目录>]
# 或批量处理
node scripts/analyze_blogger.mjs --batch <链接文件> [--output <输出目录>]
```

### 输入格式

视频链接文件，每行一个：
```
https://example.com/video/VIDEO_ID
```

### 输出

```
输出目录/
├── 文案风格.md          # 合并精炼版
├── 视频脚本.md          # 合并精炼版
├── 剪辑逻辑.md          # 合并精炼版
├── 选题策略.md          # 合并精炼版
├── 运营策略.md          # 合并精炼版
├── 跨视频校验.md        # 一致性校验报告
├── 单视频分析/          # 每个视频各维度的独立分析
├── 文案/                # 语音转文字（已标点分段）
└── 视频保留/            # 新手期保留原始视频
```

## 工作流程

```
视频链接
    ↓
Camoufox 反检测浏览器 → 平台 API → CDN 直链 → 下载
    ↓
ffmpeg 抽帧 + Whisper 语音识别
    ↓
交叉验证：Whisper + 字幕 API + RapidOCR 硬字幕
    ↓
FunASR 本地模型 → 自动标点 + 分段
    ↓
5 个专用 Prompt 多维度蒸馏分析
    ↓
最强 LLM + 思考模式 → 合并精炼输出 → .md 文件
```

## 蒸馏维度

| 维度 | 提取内容 |
|------|---------|
| 文案风格 | 语气、节奏、金句模式、开场钩子、情绪操控 |
| 视频脚本 | 结构模板、情绪曲线、段落逻辑、叙事方式 |
| 剪辑逻辑 | 节奏控制、转场规律、B-roll时机、高潮设计 |
| 选题策略 | 内容定位、受众痛点、标题公式、系列化 |
| 运营策略 | 发布节奏、互动方式、引流策略、商业化路径 |

## 防封策略

- Camoufox 反检测浏览器，C++ 层指纹伪装
- 视频间随机延迟 3-8 秒
- 单日总量上限 50 个视频
