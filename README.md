# 🎬 视频炼化

从视频创作者的内容中提取隐性经验，蒸馏为可复用的创作方法论。

> **核心理念**：不是整理"视频讲了什么"，而是提炼"创作者是怎么做的" → 可复用的创作方法论

## ✨ 核心特性

- 🌐 **多平台支持** — 抖音 / B站 / 其他平台，自动智能路由
- 🔒 **反检测下载** — 使用 [Camoufox](https://github.com/daijro/camoufox) 反检测浏览器，C++ 层指纹伪装
- **优先字幕**：有 CC 字幕或硬字幕时，直接使用字幕文本，跳过 Whisper 转文字
- **兜底方案**：无字幕时才使用 Whisper medium 语音识别
- ✍️ **文案纠正** — [FunASR](https://github.com/modelscope/FunASR) CT-Transformer 本地模型自动添加标点和分段
- 🔍 **硬字幕 OCR** — [RapidOCR](https://github.com/RapidAI/RapidOCR) 本地引擎提取画面字幕，1秒1帧全覆盖
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

### 平台下载工具（v6.1）

| 工具 | 安装方式 | 用途 |
|------|------|------|
| **opencli** | [安装 OpenCLI](https://github.com/opencli/opencli) | 首选：博主解析、元数据、评论、下载 |
| **mcporter** | `npm install -g mcporter` | 抖音无水印下载（OpenCLI 不可用时） |
| **douyin-mcp-server** | `pip install douyin-mcp-server` | 抖音 MCP 服务 |
| **yt-dlp** | `pip install yt-dlp` | B站备用下载 |
| **camoufox** | `pip install camoufox && python -m camoufox fetch` | 通用降级方案 |

> **OpenCLI 首选策略**：Chrome 打开且扩展连接时，所有平台优先使用 OpenCLI（数据更丰富、速度更快）。OpenCLI 不可用时自动降级到原有方案。

## 🚀 使用

### 博主主页分析（推荐）

```bash
# 抖音博主
node scripts/analyze_blogger.mjs --blogger "https://www.douyin.com/user/xxx"

# B站博主
node scripts/analyze_blogger.mjs --blogger "https://space.bilibili.com/xxx"

# 指定输出目录
node scripts/analyze_blogger.mjs --blogger "https://..." --output ~/Desktop/分析结果
```

### 单视频分析

```bash
# 抖音视频
node scripts/analyze_blogger.mjs "https://www.douyin.com/video/xxx"

# B站视频
node scripts/analyze_blogger.mjs "https://www.bilibili.com/video/BVxxx"

# 其他平台
node scripts/analyze_blogger.mjs "https://..."
```

### 批量分析

创建视频链接文件 `videos.txt`：
```
https://www.douyin.com/video/VIDEO_ID_1
https://www.bilibili.com/video/BVxxx
https://other-platform.com/VIDEO_ID_3
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
├── 原始数据/            # 下载的视频文件
├── 视频保留/            # 新手期保留原始视频
└── 进度.json            # 处理进度记录
```

## 🎯 蒸馏维度

| 维度 | 提取内容 |
|------|---------|
| **文案风格** | 语气、节奏、金句模式、开场钩子、情绪操控 |
| **视频脚本** | 结构模板、情绪曲线、段落逻辑、叙事方式 |
| **剪辑逻辑** | 节奏控制、转场规律、B-roll时机、高潮设计 |
| **选题策略** | 内容定位、受众痛点、标题公式、系列化 |
| **运营策略** | 发布节奏、互动方式、引流策略、商业化路径 |

## 🔄 工作流程（v7）

```
给博主主页链接 或 视频链接
    ↓
blogger_parser.py 解析
    ├── 抖音博主 → OpenCLI 首选（opencli douyin user-videos）→ 降级 Camoufox
    ├── B站博主 → OpenCLI 首选（opencli bilibili user-videos）→ 降级 yt-dlp
    └── 其他博主 → Camoufox 通用解析
    ↓
download_router.py 智能下载路由器
    ├── 抖音 → MCP 无水印下载（mcporter）→ OpenCLI → 降级 Camoufox
    ├── B站 → OpenCLI 下载 → 降级 yt-dlp（支持 1080p，需 Chrome Cookie）
    └── 其他 → Camoufox 直接下载
    ↓
ffmpeg 抽帧（1秒1帧，全覆盖）
    ↓
优先检测 CC 字幕 / 硬字幕 OCR
    ├── 有字幕？✅ 直接使用字幕文本，跳过 Whisper
    └── 无字幕？🎙️ Whisper 语音识别（本地 medium 模型）
    ↓
FunASR 本地模型 → 自动标点 + 分段
    ↓
5 个专用 Prompt 多维度蒸馏分析
    ↓
最强 LLM + 思考模式 → 合并精炼输出 → .md 文件
```

> **v7 文案提取优化**：有字幕的视频直接跳过 Whisper 转录，节省 80% 时间。硬字幕 OCR 改为 1秒1帧全覆盖，确保字幕完整性。

## 🌐 平台支持

| 平台 | 主页解析 | 视频下载 | 元数据 | 评论 |
|------|---------|---------|--------|------|
| **抖音** | ✅ OpenCLI / Camoufox | ✅ MCP / OpenCLI | ✅ | ✅ |
| **B站** | ✅ OpenCLI / yt-dlp | ✅ OpenCLI / yt-dlp | ✅ | ✅ |
| **其他** | ✅ Camoufox | ✅ Camoufox | ❌ |  |

> OpenCLI 需要 Chrome 浏览器 + 扩展实时连接，适合手动触发场景（你在电脑前时）。后台全自动运行时自动降级到 Camoufox/yt-dlp。

## ⚙️ 模型要求

云端 LLM 需要**同时具备以下能力**：
- ✅ **视觉识别**（多模态）— 能理解帧图内容
- ✅ **思考模式**（thinking/reasoning）— 深度分析能力
- ✅ **长上下文**（≥ 128K）— 支持多视频合并分析

> 推荐模型示例：GPT-4o、Claude 3.5 Sonnet、Gemini 2.5 Pro、qwen3.6-plus 等。
> 脚本支持通过环境变量或配置切换模型，请根据实际情况修改。

## 📊 运行时间与 API 调用估算

以下数据基于 **Mac mini (Apple Silicon M4)** 实测。

> 💡 **v7 优化亮点**：有字幕的视频直接跳过 Whisper 转录，节省 80% 时间。

| 场景 | 总时长（预估） | Whisper 本地耗时 | 云端 API 调用次数 |
|:------|:-------------:|:----------------:|:------------------:|
| **有字幕视频** | 显著减少 | **跳过**（0 分钟）| 不变 |
| **无字幕视频** | 较长 | ~1.63x 实时 | 不变 |
| **5 个视频** | ~1 - 2 小时 | ~0 - 60 分钟 | **30 次**（25 蒸馏 + 5 合并）|
| **10 个视频** | ~3 - 6 小时 | ~0 - 3 小时 | **55 次**（50 蒸馏 + 5 合并）|

### API 调用详解

- **单视频蒸馏**：每个视频 × 5 个维度 = **5 次调用**
- **合并精炼**：5 个维度各 1 次 = **5 次调用**
- **总调用数** = `视频数 × 5 + 5`

每个维度调用会发送：提示词 + 视频信息 + 文案 + 帧图（多模态请求）。

> ⚠️ 以上时间为**串行运行**的预估值。实际速度取决于：
> - 视频平均时长（以上数据基于 5-20 分钟/视频）
> - 是否有字幕（有字幕跳过 Whisper，大幅节省时间）
> - Mac 型号和 CPU 性能
> - 云端模型响应速度
> - 网络状况

## ⚠️ 注意事项

- 需要配置代理访问国内平台 CDN
- 单日建议不超过 50 个视频
- 视频间建议保持 3-8 秒随机延迟
- B站下载需要 Chrome 浏览器 Cookie（自动读取）

## 📄 许可证

MIT License

## 📝 版本历史

- **v7 (2026-05-15)** — 文案提取优化：优先使用 CC 字幕/硬字幕 OCR，有字幕时跳过 Whisper；硬字幕 OCR 改为 1秒1帧全覆盖
- **v6.1 (2026-05-13)** — OpenCLI 首选策略：博主解析、元数据提取、评论获取、视频下载全面集成 OpenCLI，保留原有方案作为降级
- **v6 (2026-05-11)** — 新增多平台支持（抖音 MCP 无水印 / B站 yt-dlp），新增博主主页解析器
- **v5.1 (2026-05-10)** — Mac mini Apple Silicon 调优：Whisper `compute_type=int8`，`medium` 模型最优
- **v5 (2026-05-10)** — 常驻 Whisper 服务 + 蒸馏间隔延迟 + 保留视频不删除
- **v4** — Camoufox 反检测 + 交叉验证 + 合并精炼输出
