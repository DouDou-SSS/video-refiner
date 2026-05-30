from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .config import AppConfig
from .utils import run_command


LogFn = Callable[[str, str], None]


def detect_platform(value: str) -> str:
    if "douyin.com" in value or re.fullmatch(r"\d{15,}", value):
        return "douyin"
    if "bilibili.com" in value or "b23.tv" in value or re.fullmatch(r"BV[A-Za-z0-9]{10}", value):
        return "bilibili"
    return "other"


def extract_video_id(value: str) -> str:
    for pattern in [r"modal_id=(\d+)", r"/video/(\d+)", r"(BV[A-Za-z0-9]{10})", r"av(\d+)"]:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    if re.fullmatch(r"\d{15,}", value) or re.fullmatch(r"BV[A-Za-z0-9]{10}", value):
        return value
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")[:80] or "video"


def normalize_video_url(value: str) -> str:
    if re.fullmatch(r"\d{15,}", value):
        return f"https://www.douyin.com/video/{value}"
    if re.fullmatch(r"BV[A-Za-z0-9]{10}", value):
        return f"https://www.bilibili.com/video/{value}"
    return value


def is_blogger_url(value: str) -> bool:
    return ("/user/" in value or "/@" in value) and "douyin.com" in value or "space.bilibili.com" in value


def safe_path_name(value: str, fallback: str = "未命名博主") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:80] or fallback


def resolve_blogger_name(url: str) -> str:
    platform = detect_platform(url)
    if platform == "douyin":
        return _resolve_douyin_blogger_name(url)
    if platform == "bilibili":
        return _resolve_bilibili_blogger_name(url)
    return safe_path_name(extract_video_id(url), "未知博主")


def _resolve_douyin_blogger_name(url: str) -> str:
    match = re.search(r"/user/([^?&]+)", url)
    sec_uid = match.group(1) if match else extract_video_id(url)
    fallback = f"douyin_{sec_uid[:12]}"
    name = _resolve_browser_profile_name(
        url,
        "douyin",
        """
        (() => {
          const h1 = [...document.querySelectorAll('h1')]
            .map((item) => item.innerText.trim())
            .find(Boolean);
          const meta = document.querySelector('meta[name="description"]')?.content || '';
          const title = document.title || '';
          const fromMeta = meta.split(/[。\\.]/)[0];
          const fromTitle = title.replace(/的抖音.*/, '').trim();
          return h1 || fromMeta || fromTitle || '';
        })()
        """,
    )
    return safe_path_name(name, fallback)


def _resolve_bilibili_blogger_name(url: str) -> str:
    match = re.search(r"space\.bilibili\.com/(\d+)", url)
    uid = match.group(1) if match else extract_video_id(url)
    fallback = f"bilibili_{uid}"
    name = _resolve_browser_profile_name(
        url,
        "bilibili",
        """
        (() => {
          const selectors = ['.nickname', '.h-name', '#h-name'];
          for (const selector of selectors) {
            const text = document.querySelector(selector)?.textContent?.trim();
            if (text) return text;
          }
          return (document.title || '').replace(/的个人空间.*/, '').replace(/- 哔哩哔哩.*/, '').trim();
        })()
        """,
    )
    return safe_path_name(name, fallback)


def _resolve_browser_profile_name(url: str, platform: str, js: str) -> str:
    if not shutil.which("opencli"):
        return ""
    session = "vr_profile_" + platform + "_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    try:
        opened = run_command(["opencli", "browser", session, "open", url], timeout=45)
        if opened.returncode != 0:
            return ""
        run_command(["opencli", "browser", session, "wait", "time", "3"], timeout=10)
        result = run_command(["opencli", "browser", session, "eval", js], timeout=20)
        if result.returncode != 0:
            return ""
        return _parse_opencli_eval_string(result.stdout)
    except Exception:
        return ""


def _parse_opencli_eval_string(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    try:
        value = json.loads(text)
        return str(value).strip() if value else ""
    except json.JSONDecodeError:
        pass
    return text.strip().strip('"').strip()


def opencli_connected() -> bool:
    if not shutil.which("opencli"):
        return False
    try:
        result = run_command(["opencli", "doctor"], timeout=10)
        output = result.stdout + result.stderr
        return ("Extension: connected" in output or "[OK] Extension:" in output) and (
            "Connectivity: connected" in output or "[OK] Connectivity:" in output
        )
    except Exception:
        return False


def parse_inputs(input_type: str, inputs: list[str], max_videos: int, log: LogFn) -> list[dict[str, Any]]:
    if input_type in {"single", "batch"}:
        rows = []
        for raw in inputs:
            value = raw.strip()
            if not value:
                continue
            url = normalize_video_url(value)
            rows.append({"url": url, "video_id": extract_video_id(url), "platform": detect_platform(url)})
        return rows[:max_videos]

    all_rows: list[dict[str, Any]] = []
    for url in inputs:
        platform = detect_platform(url)
        if platform == "douyin":
            all_rows.extend(_parse_douyin_blogger(url, max_videos - len(all_rows), log))
        elif platform == "bilibili":
            all_rows.extend(_parse_bilibili_blogger(url, max_videos - len(all_rows), log))
        else:
            raise RuntimeError("v1 仅支持抖音/B站博主主页解析；其他平台请使用批量视频链接。")
        if len(all_rows) >= max_videos:
            break
    return all_rows[:max_videos]


def _parse_douyin_blogger(url: str, limit: int, log: LogFn) -> list[dict[str, Any]]:
    if not opencli_connected():
        raise RuntimeError("抖音博主主页解析需要 OpenCLI Chrome 扩展连接；直接视频链接不受影响。")
    match = re.search(r"/user/([^?&]+)", url)
    if not match:
        raise RuntimeError("无法从抖音主页 URL 提取 sec_uid。")
    sec_uid = match.group(1)
    opencli_limit = min(limit, 20)
    if limit > opencli_limit:
        log("warn", f"OpenCLI 抖音主页解析单次最多返回 20 个视频，本次请求 {limit} 个，将先解析最多 {opencli_limit} 个。")
    result = run_command(
        ["opencli", "douyin", "user-videos", sec_uid, "--limit", str(opencli_limit), "--format", "json"],
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:500])
    start = result.stdout.find("[")
    if start < 0:
        raise RuntimeError("OpenCLI 未返回 JSON 视频列表。")
    data = _parse_opencli_json_list(result.stdout[start:])
    rows = []
    for item in data:
        video_id = item.get("aweme_id") or item.get("video_id")
        if not video_id:
            continue
        rows.append(
            {
                "url": f"https://www.douyin.com/video/{video_id}",
                "video_id": video_id,
                "platform": "douyin",
                "title": item.get("title") or item.get("desc") or "",
                "play_url": _first_url(item.get("play_url") or item.get("download_url") or ""),
            }
        )
    log("info", f"OpenCLI 解析抖音主页得到 {len(rows)} 个视频")
    if len(rows) < limit:
        rows = _merge_video_rows(rows, _parse_douyin_blogger_by_browser_scroll(url, limit, {str(row["video_id"]) for row in rows}, log))
    if len(rows) < limit:
        raise RuntimeError(
            f"请求解析 {limit} 个抖音视频，但当前固定解析阶梯只拿到 {len(rows)} 个。"
            "请确认 Chrome 已登录该账号可见更多作品，或改用批量视频链接补足。"
        )
    return rows[:limit]


def _merge_video_rows(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {str(row.get("video_id")) for row in base}
    rows = list(base)
    for row in extra:
        video_id = str(row.get("video_id") or "")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        rows.append(row)
    return rows


def _parse_douyin_blogger_by_browser_scroll(url: str, limit: int, seen_ids: set[str], log: LogFn) -> list[dict[str, Any]]:
    if not shutil.which("opencli"):
        return []
    session = "vr_parse_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    log("info", f"OpenCLI 列表不足，启动浏览器滚动采集补足到 {limit} 个视频")
    opened = run_command(["opencli", "browser", session, "open", url], timeout=45)
    if opened.returncode != 0:
        log("warn", f"浏览器滚动采集无法打开主页：{(opened.stderr or opened.stdout).strip()[-300:]}")
        return []
    run_command(["opencli", "browser", session, "wait", "time", "3"], timeout=10)

    rows: list[dict[str, Any]] = []
    max_rounds = max(12, min(45, limit * 2))
    no_growth_rounds = 0
    last_total = 0
    collect_js = r"""
    (() => {
      const rows = [];
      const seen = new Set();
      const push = (href, title) => {
        const text = String(href || '');
        const match = text.match(/(?:\/video\/|modal_id=)(\d{15,})/);
        if (!match || seen.has(match[1])) return;
        seen.add(match[1]);
        rows.push({
          video_id: match[1],
          url: `https://www.douyin.com/video/${match[1]}`,
          title: String(title || '').replace(/\s+/g, ' ').trim()
        });
      };
      document.querySelectorAll('a[href]').forEach((item) => {
        push(item.href, item.innerText || item.getAttribute('aria-label') || item.title || '');
      });
      performance.getEntriesByType('resource').forEach((entry) => push(entry.name, ''));
      return rows;
    })()
    """
    for _ in range(max_rounds):
        result = run_command(["opencli", "browser", session, "eval", collect_js], timeout=20)
        if result.returncode == 0:
            try:
                for item in _parse_opencli_json_list(result.stdout):
                    video_id = str(item.get("video_id") or "")
                    if not video_id or video_id in seen_ids:
                        continue
                    seen_ids.add(video_id)
                    rows.append(
                        {
                            "url": normalize_video_url(video_id),
                            "video_id": video_id,
                            "platform": "douyin",
                            "title": item.get("title") or "",
                        }
                    )
            except Exception as exc:
                log("warn", f"浏览器滚动采集结果解析失败：{exc}")
        total = len(seen_ids)
        if total >= limit:
            break
        if total == last_total:
            no_growth_rounds += 1
        else:
            no_growth_rounds = 0
            last_total = total
        if no_growth_rounds >= 5:
            break
        run_command(["opencli", "browser", session, "eval", "window.scrollTo(0, document.body.scrollHeight); document.body.scrollHeight"], timeout=10)
        run_command(["opencli", "browser", session, "wait", "time", "1.2"], timeout=10)
    log("info", f"浏览器滚动采集补充 {len(rows)} 个视频")
    return rows


def _parse_bilibili_blogger(url: str, limit: int, log: LogFn) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if opencli_connected():
        match = re.search(r"space\.bilibili\.com/(\d+)", url)
        if not match:
            raise RuntimeError("无法从 B 站主页 URL 提取 UID。")
        result = run_command(
            ["opencli", "bilibili", "user-videos", match.group(1), "--limit", str(limit), "--format", "json"],
            timeout=90,
        )
        if result.returncode == 0:
            start = result.stdout.find("[")
            data = json.loads(result.stdout[start:]) if start >= 0 else []
            for item in data:
                item_url = item.get("url") or ""
                video_id = extract_video_id(item_url)
                if video_id:
                    rows.append(
                        {
                            "url": normalize_video_url(video_id),
                            "video_id": video_id,
                            "platform": "bilibili",
                            "title": item.get("title") or "",
                        }
                    )
            if rows:
                log("info", f"OpenCLI 解析 B 站主页得到 {len(rows)} 个视频")
                return rows[:limit]

    if not shutil.which("yt-dlp"):
        raise RuntimeError("B 站主页解析需要 OpenCLI 或 yt-dlp。")
    result = run_command(
        ["yt-dlp", "--flat-playlist", "--cookies-from-browser", "chrome", "--no-warnings", "--print", "%(id)s", url],
        timeout=180,
    )
    for line in result.stdout.splitlines():
        value = line.strip()
        if value.startswith("BV"):
            rows.append({"url": normalize_video_url(value), "video_id": value, "platform": "bilibili"})
    log("info", f"yt-dlp 解析 B 站主页得到 {len(rows)} 个视频")
    return rows[:limit]


def download_video(
    url: str,
    output_path: Path,
    log: LogFn,
    api_key: str | None = None,
    source_urls: list[str] | None = None,
    max_videos: int = 50,
) -> dict[str, Any]:
    platform = detect_platform(url)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if platform == "douyin":
        return _download_douyin(url, output_path, log, api_key, source_urls or [], max_videos)
    if platform == "bilibili":
        return _download_bilibili(url, output_path, log)
    return _download_ytdlp(url, output_path, platform, log)


def _download_douyin(
    url: str,
    output_path: Path,
    log: LogFn,
    api_key: str | None = None,
    source_urls: list[str] | None = None,
    max_videos: int = 50,
) -> dict[str, Any]:
    errors: list[tuple[str, str]] = []

    if shutil.which("mcporter"):
        try:
            log("info", "抖音下载阶梯 1/4：MCP 解析直链")
            env = os.environ.copy()
            if api_key:
                env["DASHSCOPE_API_KEY"] = api_key
            result = run_command(
                ["mcporter", "call", "douyin.get_douyin_download_link", "--share_link", url],
                timeout=45,
                env=env,
            )
            if result.returncode == 0:
                raw = json.loads(result.stdout)
                data = json.loads(raw["result"]) if isinstance(raw.get("result"), str) else raw
                if data.get("status") == "error":
                    raise RuntimeError(data.get("error") or "MCP 返回错误")
                download_url = data.get("download_url")
                if download_url:
                    _curl_download(download_url, output_path)
                    return {
                        "platform": "douyin",
                        "method": "mcporter",
                        "title": data.get("title") or "",
                        "size_mb": round(output_path.stat().st_size / 1024 / 1024, 2),
                    }
                raise RuntimeError("MCP 未返回 download_url")
        except Exception as exc:
            errors.append(("MCP 直链", str(exc)))
            log("warn", f"MCP 下载失败，继续降级：{exc}")
    else:
        errors.append(("MCP 直链", "未安装 mcporter"))

    for name, downloader in [
        ("OpenCLI 博主列表直链", lambda: _download_douyin_from_blogger_play_url(url, output_path, source_urls or [], max_videos, log)),
        ("yt-dlp 浏览器 cookies", lambda: _download_douyin_ytdlp(url, output_path, log)),
        ("OpenCLI 浏览器视频源", lambda: _download_douyin_browser_video(url, output_path, log)),
    ]:
        try:
            return downloader()
        except Exception as exc:
            errors.append((name, str(exc)))
            log("warn", f"{name} 下载失败，继续降级：{exc}")

    details = "\n".join(f"- {name}: {message}" for name, message in errors)
    raise RuntimeError(f"抖音下载所有固定阶梯失败：\n{details}")


def _download_douyin_from_blogger_play_url(
    url: str,
    output_path: Path,
    source_urls: list[str],
    max_videos: int,
    log: LogFn,
) -> dict[str, Any]:
    log("info", "抖音下载阶梯 2/4：OpenCLI 博主列表直链")
    if not shutil.which("opencli"):
        raise RuntimeError("未安装 opencli")
    current_id = extract_video_id(url)
    blogger_urls = [item for item in source_urls if "douyin.com" in item and "/user/" in item]
    if not blogger_urls:
        raise RuntimeError("当前任务没有可用于回查直链的抖音博主主页输入")

    errors: list[str] = []
    for blogger_url in blogger_urls:
        match = re.search(r"/user/([^?&]+)", blogger_url)
        if not match:
            errors.append(f"{blogger_url}: 无法提取 sec_uid")
            continue
        sec_uid = match.group(1)
        limit = 20
        result = run_command(
            ["opencli", "douyin", "user-videos", sec_uid, "--limit", str(limit), "--format", "json"],
            timeout=120,
        )
        if result.returncode != 0:
            errors.append((result.stderr or result.stdout or "OpenCLI 查询失败").strip()[-500:])
            continue
        try:
            data = _parse_opencli_json_list(result.stdout)
        except Exception as exc:
            errors.append(f"OpenCLI 返回无法解析：{exc}")
            continue
        for item in data:
            item_id = str(item.get("aweme_id") or item.get("video_id") or "")
            if item_id != current_id:
                continue
            play_url = _first_url(item.get("play_url") or item.get("download_url"))
            if not play_url:
                raise RuntimeError(f"找到视频 {current_id}，但 OpenCLI 未返回 play_url")
            _curl_download(play_url, output_path)
            return {
                "platform": "douyin",
                "method": "opencli-user-videos-play-url",
                "title": item.get("title") or item.get("desc") or "",
                "size_mb": round(output_path.stat().st_size / 1024 / 1024, 2),
            }
        errors.append(f"{sec_uid}: 最近 {limit} 条未找到 {current_id}")

    raise RuntimeError("；".join(errors) if errors else f"未找到视频 {current_id} 的博主直链")


def _download_douyin_ytdlp(url: str, output_path: Path, log: LogFn) -> dict[str, Any]:
    log("info", "抖音下载阶梯 3/4：yt-dlp + Chrome cookies")
    if not shutil.which("yt-dlp"):
        raise RuntimeError("未安装 yt-dlp")
    if output_path.exists() and output_path.stat().st_size <= 1024:
        output_path.unlink()
    cmd = [
        "yt-dlp",
        "--cookies-from-browser",
        "chrome",
        "-f",
        "best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        str(output_path),
        url,
    ]
    proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy:
        cmd[1:1] = ["--proxy", proxy]
    result = run_command(cmd, timeout=900)
    if output_path.exists() and output_path.stat().st_size > 1024:
        return {
            "platform": "douyin",
            "method": "yt-dlp-cookies",
            "size_mb": round(output_path.stat().st_size / 1024 / 1024, 2),
        }
    raise RuntimeError((result.stderr or result.stdout or "yt-dlp 下载失败").strip()[-1000:])


def _download_douyin_browser_video(url: str, output_path: Path, log: LogFn) -> dict[str, Any]:
    log("info", "抖音下载阶梯 4/4：OpenCLI 浏览器视频源")
    if not shutil.which("opencli"):
        raise RuntimeError("未安装 opencli")
    session = "vr_video_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    opened = run_command(["opencli", "browser", session, "open", url], timeout=45)
    if opened.returncode != 0:
        raise RuntimeError((opened.stderr or opened.stdout or "OpenCLI 打开页面失败").strip()[-500:])
    run_command(["opencli", "browser", session, "wait", "time", "5"], timeout=15)
    js = """
    (() => {
      const fromVideo = [...document.querySelectorAll('video')]
        .map((video) => video.currentSrc || video.src || [...video.querySelectorAll('source')].map((source) => source.src).find(Boolean) || '')
        .find((src) => /^https?:/.test(src));
      if (fromVideo) return fromVideo;
      return performance.getEntriesByType('resource')
        .map((entry) => entry.name)
        .find((name) => /^https?:/.test(name) && /douyinvod|video_mp4|mime_type=video_mp4/.test(name)) || '';
    })()
    """
    result = run_command(["opencli", "browser", session, "eval", js], timeout=20)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "OpenCLI 读取视频源失败").strip()[-500:])
    video_src = _parse_opencli_eval_string(result.stdout)
    if not video_src.startswith(("http://", "https://")):
        raise RuntimeError("浏览器页面没有暴露可下载的 http(s) 视频源")
    _curl_download(video_src, output_path)
    return {
        "platform": "douyin",
        "method": "opencli-browser-video-src",
        "size_mb": round(output_path.stat().st_size / 1024 / 1024, 2),
    }


def _download_bilibili(url: str, output_path: Path, log: LogFn) -> dict[str, Any]:
    if opencli_connected():
        video_id = extract_video_id(url)
        result = run_command(["opencli", "bilibili", "download", video_id], timeout=600, cwd=output_path.parent)
        if result.returncode == 0 and output_path.exists():
            return {
                "platform": "bilibili",
                "method": "opencli",
                "size_mb": round(output_path.stat().st_size / 1024 / 1024, 2),
            }
        log("warn", "OpenCLI B 站下载未生成目标文件，降级到 yt-dlp")
    return _download_ytdlp(url, output_path, "bilibili", log)


def _download_ytdlp(url: str, output_path: Path, platform: str, log: LogFn) -> dict[str, Any]:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("未安装 yt-dlp。")
    cmd = [
        "yt-dlp",
        "-f",
        "best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        str(output_path),
        url,
    ]
    proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy:
        cmd[1:1] = ["--proxy", proxy]
    result = run_command(cmd, timeout=900)
    if output_path.exists() and output_path.stat().st_size > 1024:
        return {
            "platform": platform,
            "method": "yt-dlp",
            "size_mb": round(output_path.stat().st_size / 1024 / 1024, 2),
        }
    raise RuntimeError((result.stderr or result.stdout or "yt-dlp 下载失败").strip()[-1000:])


def _parse_opencli_json_list(raw: str) -> list[dict[str, Any]]:
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError("OpenCLI 未返回 JSON 列表")
    data = json.loads(raw[start : end + 1])
    if not isinstance(data, list):
        raise RuntimeError("OpenCLI JSON 不是列表")
    return [item for item in data if isinstance(item, dict)]


def _first_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            url = _first_url(item)
            if url:
                return url
    if isinstance(value, dict):
        for key in ["url", "play_url", "download_url", "main_url"]:
            url = _first_url(value.get(key))
            if url:
                return url
    return ""


def _curl_download(url: str, output_path: Path) -> None:
    part_path = output_path.with_suffix(output_path.suffix + ".part")
    if part_path.exists():
        part_path.unlink()
    headers = [
        "-H",
        "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "-H",
        "Referer: https://www.douyin.com/",
    ]
    result = run_command(
        [
            "curl",
            "-L",
            "--fail",
            "--retry",
            "5",
            "--retry-all-errors",
            "--connect-timeout",
            "20",
            "--speed-time",
            "60",
            "--speed-limit",
            "1024",
            "--compressed",
            *headers,
            "-o",
            str(part_path),
            url,
        ],
        timeout=900,
    )
    if result.returncode == 0 and part_path.exists() and part_path.stat().st_size > 1024:
        part_path.replace(output_path)
        return
    if part_path.exists():
        part_path.unlink()
    raise RuntimeError((result.stderr or result.stdout or "curl 下载失败").strip()[-1000:])


def extract_frames(config: AppConfig, video_path: Path, frames_dir: Path, frame_interval_seconds: int | None = None) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_pattern = frames_dir / "frame_%04d.jpg"
    interval = max(1, int(frame_interval_seconds or 1))
    fps_expr = f"fps=1/{interval}" if interval > 1 else f"fps={config.frame_fps}"
    result = run_command(
        [config.ffmpeg_bin, "-y", "-i", str(video_path), "-vf", fps_expr, "-q:v", "2", str(frame_pattern)],
        timeout=300,
    )
    frames = sorted(frames_dir.glob("*.jpg"))
    if result.returncode != 0 or not frames:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg 抽帧失败").strip()[-1000:])
    return frames
