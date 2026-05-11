#!/usr/bin/env python3
"""
博主主页视频解析器 - 使用 Camoufox 提取所有视频 ID

用法:
  python blogger_parser.py <博主主页URL>

输出: JSON，包含 video_ids 列表和 count

解析策略:
  - 抖音博主 → Camoufox 打开页面 + 监听 API 响应 + 滚动加载
  - B站博主  → yt-dlp --flat-playlist --cookies-from-browser chrome (B站有 WBI 签名)
  - 其他平台 → Camoufox 通用解析（打开页面 + 滚动 + 提取视频链接）
"""

import sys
import json
import os
import re
import subprocess
import time

from camoufox.sync_api import Camoufox


def detect_platform(url):
    """检测平台"""
    if 'douyin.com' in url:
        return 'douyin'
    elif 'bilibili.com' in url or 'b23.tv' in url:
        return 'bilibili'
    else:
        return 'other'


def detect_input_type(url_or_id):
    """判断输入是博主主页还是单个视频"""
    if url_or_id.startswith('http'):
        platform = detect_platform(url_or_id)
        if platform == 'douyin':
            if '/user/' in url_or_id or '/@' in url_or_id:
                return 'blogger'
            return 'single_video'
        elif platform == 'bilibili':
            if 'space.bilibili.com/' in url_or_id:
                return 'blogger'
            return 'single_video'
        else:
            # 其他平台：默认认为是博主主页（可通过 --single 参数指定为单个视频）
            return 'blogger'
    else:
        if re.match(r'^\d{15,}$', url_or_id):
            return 'douyin_id'
        elif re.match(r'^BV[A-Za-z0-9]{10}$', url_or_id):
            return 'bilibili_id'
        return 'unknown'


# ========================
# 抖音博主解析
# ========================
def parse_douyin_blogger(url):
    """抖音博主 → Camoufox + API 监听"""
    video_ids = []

    with Camoufox(headless=True) as browser:
        context = browser.new_context()
        page = context.new_page()

        def handle_response(response):
            nonlocal video_ids
            try:
                if '/aweme/v1/web/aweme/post/' in response.url and response.status == 200:
                    data = response.json()
                    aweme_list = data.get('aweme_list', [])
                    for aweme in aweme_list:
                        video_id = aweme.get('aweme_id', '')
                        if video_id and video_id not in video_ids:
                            video_ids.append(video_id)
            except Exception:
                pass

        page.on('response', handle_response)

        try:
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            # 滚动加载更多内容
            for _ in range(5):
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(3000)
        except Exception as e:
            return {'error': str(e)}

    return {'video_ids': video_ids, 'count': len(video_ids)}


# ========================
# B站博主解析
# ========================
def parse_bilibili_blogger(url):
    """B站博主 → yt-dlp（处理 WBI 签名）"""
    try:
        proxy = os.environ.get('http_proxy', '') or os.environ.get('https_proxy', '')
        cmd = [
            'yt-dlp',
            '--flat-playlist',
            '--cookies-from-browser', 'chrome',
            '--no-warnings',
            '--print', '%(id)s',
            url
        ]
        if proxy:
            cmd.insert(2, '--proxy')
            cmd.insert(3, proxy)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip().startswith('BV')]
        return {'video_ids': lines, 'count': len(lines)}
    except subprocess.TimeoutExpired:
        return {'error': 'yt-dlp 获取 B 站视频列表超时'}
    except Exception as e:
        return {'error': f'yt-dlp 异常: {str(e)}'}


# ========================
# 通用博主解析（其他平台）
# ========================
def parse_generic_blogger(url):
    """其他平台博主 → Camoufox 通用解析

    策略：
    1. 打开博主主页
    2. 滚动加载
    3. 监听 API 响应（如果有）
    4. 提取页面中所有视频链接
    """
    video_ids = []
    video_urls = []

    with Camoufox(headless=True) as browser:
        context = browser.new_context()
        page = context.new_page()

        # 策略 A: 监听 API 响应
        def handle_response(response):
            nonlocal video_ids
            try:
                if response.status == 200:
                    content_type = response.headers.get('content-type', '')
                    if 'application/json' in content_type:
                        data = response.json()
                        # 递归查找 video_id, aweme_id, vid, bvid 等字段
                        _extract_ids_recursive(data, video_ids)
            except Exception:
                pass

        page.on('response', handle_response)

        try:
            page.goto(url, wait_until='domcontentloaded', timeout=30000)

            # 滚动加载更多内容
            for _ in range(5):
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(3000)

            # 策略 B: 从页面中提取所有视频链接
            extracted = page.evaluate('''() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                const urls = [];
                const ids = [];

                for (const link of links) {
                    const href = link.href || '';

                    // 通用模式：提取所有可能的视频 ID
                    // 抖音: 19位数字
                    const douyinMatch = href.match(/(\\d{15,})/);
                    if (douyinMatch && !href.includes('space')) {
                        ids.push(douyinMatch[1]);
                    }

                    // B站: BV + 10位字符
                    const bvMatch = href.match(/(BV[A-Za-z0-9]{10})/);
                    if (bvMatch) {
                        ids.push(bvMatch[1]);
                    }

                    // 提取所有视频链接
                    if (href.match(/\\/video\\//) || href.match(/\\/watch\\?v=/) ||
                        href.match(/\\/play\\//) || href.match(/\\/item\\//)) {
                        urls.push(href);
                    }
                }

                return { urls, ids };
            }''')

            if extracted:
                for vid in extracted.get('ids', []):
                    if vid not in video_ids:
                        video_ids.append(vid)
                for vid_url in extracted.get('urls', []):
                    if vid_url not in video_urls:
                        video_urls.append(vid_url)

        except Exception as e:
            return {'error': str(e)}

    # 优先返回视频 ID，如果没有 ID 则返回 URL
    if video_ids:
        return {'video_ids': video_ids, 'count': len(video_ids)}
    elif video_urls:
        return {'video_urls': video_urls, 'count': len(video_urls)}
    else:
        return {'error': '未能从页面提取到视频 ID 或链接，可能需要平台特定的解析逻辑'}


def _extract_ids_recursive(data, ids_list):
    """递归从 JSON 数据结构中提取视频 ID"""
    if isinstance(data, dict):
        # 常见的视频 ID 字段名
        for key in ['aweme_id', 'video_id', 'vid', 'bvid', 'aid', 'id']:
            val = data.get(key)
            if val and isinstance(val, str) and len(val) >= 10:
                if val not in ids_list:
                    ids_list.append(val)
        # 递归
        for v in data.values():
            _extract_ids_recursive(v, ids_list)
    elif isinstance(data, list):
        for item in data:
            _extract_ids_recursive(item, ids_list)


# ========================
# 主函数
# ========================
def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': '用法: python blogger_parser.py <URL>'}))
        sys.exit(1)

    url_or_id = sys.argv[1]
    input_type = detect_input_type(url_or_id)

    print(f'[解析器] 输入类型: {input_type}', file=sys.stderr)
    print(f'[解析器] 输入: {url_or_id[:80]}...', file=sys.stderr)

    if input_type == 'blogger':
        platform = detect_platform(url_or_id)
        print(f'[解析器] 平台: {platform}', file=sys.stderr)

        if platform == 'douyin':
            result = parse_douyin_blogger(url_or_id)
        elif platform == 'bilibili':
            result = parse_bilibili_blogger(url_or_id)
        else:
            print('[解析器] 使用 Camoufox 通用解析', file=sys.stderr)
            result = parse_generic_blogger(url_or_id)

    elif input_type in ('single_video', 'douyin_id', 'bilibili_id'):
        result = {'video_ids': [url_or_id], 'count': 1, 'type': 'single'}

    else:
        result = {'error': f'无法识别输入: {url_or_id}'}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
