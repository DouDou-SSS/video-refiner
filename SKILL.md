---
name: video-refiner
description: 视频创作者方法论蒸馏工具。通过反检测浏览器获取视频，Whisper 语音识别 + FunASR 标点恢复提取文案，RapidOCR 本地 OCR 提取硬字幕，结合多模态大模型进行多维度创作方法论蒸馏分析，最终输出结构化的 .md 文件。
---

# 🎬 视频炼化

从视频创作者的内容中提取隐性经验，蒸馏为可复用的创作方法论。

## 核心功能

- **反检测下载** — 使用 Camoufox 反检测浏览器获取视频链接，防封控
- **语音转文字** — Whisper large-v3/medium 语音识别，**常驻服务模式**（一次加载模型，全程复用，不重复启动）
- **文案纠正** — FunASR CT-Transformer 本地模型自动添加标点符号和分段
- **硬字幕 OCR** — RapidOCR 本地引擎提取画面中的硬字幕，辅助交叉验证
- **多维度蒸馏** — 5 个专用 Prompt 分别分析文案风格、视频脚本、剪辑逻辑、选题策略、运营策略
- **蒸馏延迟** — 每个维度之间自动间隔 10-20 秒随机延迟，防止触发云端模型频率限制
- **合并精炼输出** — 使用最强 LLM + 思考模式，合并所有视频分析为精炼的 .md 文件
- **保留视频** — 原始视频不再自动删除，等用户确认后再清理

## 版本历史

- **v5.1 (2026-05-10)** — Mac mini Apple Silicon 调优：Whisper `compute_type=int8`，`medium` 模型最优
- **v5 (2026-05-10)** — 常驻 Whisper 服务 + 蒸馏间隔延迟 + 保留视频不删除
- **v4** — Camoufox 反检测 + 交叉验证 + 合并精炼输出

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
├── 视频保留/            # 新手期保留原始视频
├── 原始数据/            # 中间文件（含 Whisper 文本、抽帧临时数据、原始视频）
└── 进度.json             # 处理进度跟踪
```

> ⚠️ **v5 变更**：原始视频和临时文件不再自动删除。所有视频保留在 `原始数据/` 和 `视频保留/` 中，等用户确认输出无误后再手动清理。

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

## Whisper 配置

> ⚠️ **Mac mini（Apple Silicon）无 GPU 时的重要调优经验：**

| 配置项 | Mac mini（CPU） | Windows（NVIDIA GPU） |
|--------|----------------|----------------------|
| 模型 | `medium` | `large-v3` |
| 设备 | `cpu` | `cuda` |
| 计算类型 | `int8` | `int8` |
| 5 分钟视频耗时 | ~3.3 分钟（1.63x 实时） | ~30 秒（10x+ 实时） |

**关键教训：**
- `compute_type="auto"` 在 Mac 上实际使用 float32，速度极慢（5 分钟视频需 16 分钟）
- 必须显式指定 `compute_type="int8"`，速度提升近 5 倍
- `large-v3` 在纯 CPU 上比 `medium` 更慢（参数量 2 倍），只在 GPU 下有优势
- 文案提取流程（v7 2026-05-15）：优先检测CC字幕和硬字幕OCR，有字幕时跳过Whisper，无字幕时才用Whisper转文字
- 硬字幕OCR规则：1秒1帧（fps=1），数量不限，使用RapidOCR本地引擎
- cross_validate.py 自动决定是否需要Whisper，优先使用字幕来源
- Whisper 作为兜底方案，仅在无字幕时使用

## 防封策略

- Camoufox 反检测浏览器，C++ 层指纹伪装
- 视频间随机延迟 3-8 秒
- 单日总量上限 50 个视频

## 资料完整性规则

> **宁缺毋滥**：缺少任何核心资料（帧图、文案），跳过该视频的蒸馏分析。

核心资料包括：
- **帧图**：ffmpeg 抽帧结果（至少 1 张）
- **文案**：Whisper + 交叉验证 + 标点分段后的完整文案

如果资料不全：
- ❌ 不调用 LLM 进行蒸馏（避免产出垃圾结果，浪费 API）
- 🚫 记录跳过原因到 `进度.json`（`status: skipped`）
- 💡 修好缺失资料后，可以重新跑跳过蒸馏的视频

阶段二合并精炼时，也**只使用资料完整的视频**，跳过的视频不参与。
