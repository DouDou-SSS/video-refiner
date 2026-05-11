#!/usr/bin/env python3
"""
视频下载路由器 - 根据平台智能选择下载方式

用法:
  python download_router.py <URL> --output <path.mp4>

平台判断:
  - douyin.com     → MCP 下载无水印 (mcporter call douyin.get_douyin_download_link)
  - bilibili.com   → yt-dlp 下载
  - 其他           → Camoufox 浏览器直接下载（返回 CDN 链接后用 curl）

输出: JSON 格式，包含平台、下载方式、视频信息
"""

import sys
import json
import os
import re
import subprocess
from pathlib import Path


def detect_platform(url):
    """检测视频平台"""
    if 'douyin.com' in url:
        return 'douyin'
    elif 'bilibili.com' in url or 'b23.tv' in url:
        return 'bilibili'
    else:
        return 'other'


def get_douyin_info(video_url):
    """抖音: 使用 MCP 服务器获取视频信息"""
    try:
        result = subprocess.run(
            ['mcporter', 'call', 'douyin.parse_douyin_video_info', f'share_link={video_url}'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            inner = json.loads(data.get('result', '{}'))
            return inner
        else:
            return {'error': f'MCP 调用失败: {result.stderr}'}
    except Exception as e:
        return {'error': f'MCP 调用异常: {str(e)}'}


def download_douyin(video_url, out_path):
    """抖音: MCP 下载无水印视频"""
    print(f"  🟢 [MCP] 抖音无水印下载...", file=sys.stderr)

    # 获取下载链接
    info = get_douyin_info(video_url)
    if info.get('error'):
        return {'error': info['error']}

    download_url = info.get('download_url')
    if not download_url:
        return {'error': 'MCP 未返回下载链接'}

    # 使用 curl 下载
    proxy = os.environ.get('http_proxy', '') or os.environ.get('https_proxy', '')
    cmd = ['curl', '-sL', '-o', out_path, download_url, '-H', 'Referer: https://www.douyin.com/']
    if proxy:
        cmd = ['curl', '-sL', '-x', proxy, '-o', out_path, download_url, '-H', 'Referer: https://www.douyin.com/']

    subprocess.run(cmd, check=True, timeout=120)

    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if size < 10000:
        return {'error': f'文件太小 ({size/1024:.1f}KB)，可能下载失败'}

    info['download_size_mb'] = round(size / 1024 / 1024, 2)
    info['platform'] = 'douyin'
    info['method'] = 'mcp'
    return info


def download_bilibili(video_url, out_path):
    """B站: 使用 yt-dlp 下载"""
    print(f"  🟡 [yt-dlp] B站视频下载...", file=sys.stderr)

    # 提取 BV 号或 av 号
    bv_match = re.search(r'BV[\w]+', video_url)
    av_match = re.search(r'av(\d+)', video_url)

    if not bv_match and not av_match:
        return {'error': f'无法提取 B站视频 ID: {video_url}'}

    video_id = bv_match.group(0) if bv_match else f'av{av_match.group(1)}'

    proxy = os.environ.get('http_proxy', '') or os.environ.get('https_proxy', '')
    cmd = [
        'yt-dlp',
        '-f', 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        '-o', out_path,
        '--no-playlist',
        '--no-warnings',
        '--merge-output-format', 'mp4',
        video_url
    ]
    if proxy:
        cmd.insert(2, '--proxy')
        cmd.insert(3, proxy)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return {'error': f'yt-dlp 下载失败: {result.stderr[:500]}'}
    except subprocess.TimeoutExpired:
        return {'error': 'yt-dlp 下载超时'}

    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if size < 10000:
        return {'error': f'文件太小 ({size/1024:.1f}KB)，可能下载失败'}

    return {
        'platform': 'bilibili',
        'method': 'yt-dlp',
        'video_id': video_id,
        'download_size_mb': round(size / 1024 / 1024, 2),
    }


def download_other_with_camoufox(video_url, out_path, camoufox_python, get_video_info_py):
    """其他平台: 使用 Camoufox 浏览器获取 CDN 链接后下载"""
    print(f"  🔵 [Camoufox] 反检测浏览器获取下载链接...", file=sys.stderr)

    # 从 URL 提取视频 ID（假设是抖音格式）
    video_id_match = re.search(r'modal_id=(\d+)', video_url) or re.search(r'/video/(\d+)', video_url)
    if not video_id_match:
        return {'error': f'无法提取视频 ID: {video_url}'}

    video_id = video_id_match.group(1)

    try:
        result = subprocess.run(
            [camoufox_python, get_video_info_py, video_id],
            capture_output=True, text=True, timeout=120
        )

        if result.returncode != 0:
            return {'error': f'Camoufox 获取失败: {result.stderr[:500]}'}

        data = json.loads(result.stdout)
        if not data or not isinstance(data, list) or len(data) == 0:
            return {'error': 'Camoufox 返回空数据'}

        info = data[0]
        if info.get('error') or not info.get('playUrl'):
            return {'error': info.get('error', 'No CDN URL')}

        # 使用 curl 下载
        download_url = info['playUrl']
        proxy = os.environ.get('http_proxy', '') or os.environ.get('https_proxy', '')
        cmd = ['curl', '-sL', '-o', out_path, download_url, '-H', 'Referer: https://www.douyin.com/']
        if proxy:
            cmd = ['curl', '-sL', '-x', proxy, '-o', out_path, download_url, '-H', 'Referer: https://www.douyin.com/']

        subprocess.run(cmd, check=True, timeout=120)

        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if size < 10000:
            return {'error': f'文件太小 ({size/1024:.1f}KB)，可能下载失败'}

        info['download_size_mb'] = round(size / 1024 / 1024, 2)
        info['platform'] = 'other'
        info['method'] = 'camoufox'
        return info

    except Exception as e:
        return {'error': f'Camoufox 下载异常: {str(e)}'}


def download(url, out_path, camoufox_python=None, get_video_info_py=None):
    """统一下载入口"""
    platform = detect_platform(url)
    print(f"🎯 平台: {platform} | URL: {url[:60]}...", file=sys.stderr)

    if platform == 'douyin':
        return download_douyin(url, out_path)
    elif platform == 'bilibili':
        return download_bilibili(url, out_path)
    else:
        if not camoufox_python or not get_video_info_py:
            return {'error': '其他平台需要 Camoufox 环境，但未提供参数'}
        return download_other_with_camoufox(url, out_path, camoufox_python, get_video_info_py)


def main():
    if len(sys.argv) < 4 or sys.argv[2] != '--output':
        print("用法: python download_router.py <URL> --output <path.mp4>")
        sys.exit(1)

    url = sys.argv[1]
    out_path = sys.argv[3]

    # 可选参数
    camoufox_python = None
    get_video_info_py = None
    for i, arg in enumerate(sys.argv):
        if arg == '--camoufox-python' and i + 1 < len(sys.argv):
            camoufox_python = sys.argv[i + 1]
        elif arg == '--get-video-info-py' and i + 1 < len(sys.argv):
            get_video_info_py = sys.argv[i + 1]

    result = download(url, out_path, camoufox_python, get_video_info_py)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result.get('error'):
        sys.exit(1)


if __name__ == '__main__':
    main()
