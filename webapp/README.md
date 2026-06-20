# 视频炼化本地 Web

本目录是视频炼化的软件化 v1。目标是把原先由智能体临场执行的散装脚本，固定为本机 Web 软件中的状态机流程。

## 启动

```bash
cd /path/to/video-refiner

# 后端依赖
python3 -m venv webapp/backend/.venv
webapp/backend/.venv/bin/python -m pip install -r webapp/backend/requirements.txt

# 前端依赖与构建
cd webapp/frontend
npm install
npm run build

# 启动本地 Web
cd /path/to/video-refiner
PYTHONPATH=webapp/backend webapp/backend/.venv/bin/python -m uvicorn videorefiner_app.main:app --host 127.0.0.1 --port 7860
```

打开 `http://127.0.0.1:7860`。

## 固定流程

任务状态机固定为：

```text
预检 -> 解析输入 -> 下载视频 -> 抽帧 -> 文案提取 -> 证据时间线 -> 资料完整性检查 -> 5维单视频蒸馏 -> 5维合并精炼 -> 第6维 Benchmark Intelligence -> 完成
```

LLM 只在以下步骤中使用：

- 证据时间线的镜头视觉标注（一次生成，后续复用）
- 单视频 5 维蒸馏
- 跨视频合并精炼与 Benchmark Intelligence 汇总

LLM 不参与决定流程、不修改代码、不临场补救。

## 模型配置

Web 页面支持以下供应商：

- 阿里云百炼
- 小米 MiMo
- 火山方舟
- OpenAI
- DeepSeek
- OpenRouter
- 自定义 OpenAI-compatible

API Key 不写入数据库，不写入仓库：

- macOS 优先写入 Keychain
- Keychain 不可用时，写入 `~/.video-refiner/secure/api-keys.json.enc`

任务启动前必须选择已经通过“测试连接”的模型配置。5 维炼化依赖帧图分析，所以不支持视觉输入的模型不能启动任务。

## 输出

输出目录保持兼容旧结构：

```text
输出目录/
├── 文案风格.md
├── 视频脚本.md
├── 剪辑逻辑.md
├── 选题策略.md
├── 运营策略.md
├── manifest.json
├── 进度.json
├── evidence/            # 轻量视觉证据时间线 JSON，可在任务详情查看
├── 单视频分析/
├── 文案/
├── 原始数据/
└── 视频保留/
```

`manifest.json` 会记录软件版本、模型配置快照、Prompt hash、配置快照和产物路径。

新版任务会为每个视频生成 `evidence/{video_id}.visual_timeline.json`：包含场景变化摘要、关键帧相对路径、图文时间关联、可见画面描述与稳定 evidence ID。每个节点明确标记为 `evidence_window`（抽样分析窗口）或 `detected_cut_segment`（由场景检测得到的切段）；前者不能被当作真实镜头时长或精确切点。任务详情的已完成视频卡片可打开只读证据时间线。视觉结论不能用静态图推断音频、完整台词或连续运镜；只有 `alignment_status=timed` 且存在足够高/中置信度检测切段时，才可输出精确时序结论。模型失败或时间线未通过校验时，任务会等待自动重试，不生成占位结果。

详细设计见 [`docs/视频证据时间线优化方案.md`](../docs/视频证据时间线优化方案.md)。

## 旧脚本边界

旧 `scripts/` 保留为历史参考和兼容工具。Web v1 的主流程不调用 `scripts/analyze_blogger.mjs`，也不允许通过智能体临时生成补跑脚本来改变流程。
