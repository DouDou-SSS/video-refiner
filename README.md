# 🎬 视频炼化

从视频创作者的内容中提取隐性经验，蒸馏为可复用的创作方法论。

> **核心理念**：不是整理"视频讲了什么"，而是提炼"创作者是怎么做的" → 可复用的创作方法论

## ✨ 核心特性

- 🔒 **反检测下载** — 使用 [Camoufox](https://github.com/daijro/camoufox) 反检测浏览器，C++ 层指纹伪装
- 🎙️ **语音转文字** — Whisper large-v3/medium 语音识别，自动根据视频时长切换模型
- ✍️ **文案纠正** — [FunASR](https://github.com/modelscope/FunASR) CT-Transformer 本地模型自动添加标点和分段
- 🔍 **硬字幕 OCR** — [RapidOCR](https://github.com/RapidAI/RapidOCR) 本地引擎提取画面字幕
- 📊 **多维度蒸馏** — 5 个专用 Prompt 分别分析创作方法论的 5 个维度
- 🧠 **合并精炼** — 最强 LLM + 思考模式，合并所有视频分析为精炼的 .md 文件

## 📦 安装

### 系统依赖

```bash
# macOS
brew install ffmpeg

# 创建 Python 虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装 Python 依赖
pip install camoufox faster-whisper funasr rapidocr_onnxruntime

# 下载 Camoufox 浏览器
python -m camoufox fetch
```

## 🚀 使用

### 单视频分析

```bash
node scripts/analyze_blogger.mjs <视频ID或链接> [--output <输出目录>]
```

### 批量分析

创建视频链接文件 `videos.txt`：
```
https://example.com/video/VIDEO_ID_1
https://example.com/video/VIDEO_ID_2
https://example.com/video/VIDEO_ID_3
```

```bash
node scripts/analyze_blogger.mjs --batch videos.txt --output ./output
```

## 📁 输出结构

```
output/
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

## 🎯 蒸馏维度

| 维度 | 提取内容 |
|------|---------|
| **文案风格** | 语气、节奏、金句模式、开场钩子、情绪操控 |
| **视频脚本** | 结构模板、情绪曲线、段落逻辑、叙事方式 |
| **剪辑逻辑** | 节奏控制、转场规律、B-roll时机、高潮设计 |
| **选题策略** | 内容定位、受众痛点、标题公式、系列化 |
| **运营策略** | 发布节奏、互动方式、引流策略、商业化路径 |

## 🔄 工作流程

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

## ⚠️ 注意事项

- 需要配置代理访问国内平台 CDN
- 单日建议不超过 50 个视频
- 视频间建议保持 3-8 秒随机延迟

## 📄 许可证

MIT License
# SSH Push Test
