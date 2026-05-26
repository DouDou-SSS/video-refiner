# 视频炼化本机 Web 软件

这是一个本机单用户 Web 软件。软件在你的 Mac 上运行，浏览器访问：

```text
http://127.0.0.1:7860
```

## 首次安装

1. 解压 `video-refiner-portable.zip`。
2. 打开解压后的 `video-refiner` 文件夹。
3. 双击 `install.command`。
4. 等待依赖安装完成。
5. 双击 `start.command` 启动软件。

如果 macOS 阻止打开 `.command` 文件，可以右键文件，选择“打开”，再确认运行。

## 每次启动

双击：

```text
start.command
```

启动后会自动打开浏览器。关闭启动窗口会停止软件。

## 首次配置模型

打开页面后进入“模型”页面：

1. 选择模型供应商。
2. 填写 `API Key`、`Base URL`、蒸馏模型和合并模型。
3. 点击“保存配置”。
4. 点击“测试”。
5. 测试通过后再创建任务。

API Key 不会写入软件目录：

- macOS Keychain 可用时，优先写入 Keychain。
- Keychain 不可用时，写入本机 `~/.video-refiner/secure/api-keys.json.enc`。

## 数据保存位置

默认位置：

```text
配置和数据库：~/.video-refiner/
输出结果：~/Desktop/视频炼化输出/
```

这些内容不会放在软件包里，也不会跟随软件一起分发。

## 依赖说明

安装脚本会创建软件自己的 Python 虚拟环境，并安装运行依赖。

必须依赖：

- Python 3.10 到 3.12
- Node.js / npm
- ffmpeg
- Python 运行依赖，包括 FastAPI、OpenAI SDK、Whisper、RapidOCR、yt-dlp、PyTorch、FunASR、Camoufox
- `mcporter`：安装到软件目录的 `webapp/node-tools`，用于抖音直链解析。
- `OpenCLI`：安装到软件目录的 `webapp/node-tools`，用于博主主页解析和浏览器下载阶梯。
- `Camoufox`：安装 Python 包并执行 `python -m camoufox fetch` 拉取浏览器运行文件。

可选依赖：

- `OpenCLI` Chrome 扩展连接：命令行会自动安装，但 Chrome 扩展需要用户按 OpenCLI 提示手动安装和连接。预检会把未连接标记为失败，因为博主主页解析依赖它。

## 卸载

删除软件文件夹即可删除程序本体。

如果也要删除本机数据，再删除：

```text
~/.video-refiner/
~/Desktop/视频炼化输出/
```

如果曾保存过 API Key，也可以在 macOS Keychain 中搜索 `video-refiner` 并删除对应条目。
