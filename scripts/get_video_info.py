#!/usr/bin/env python3
"""
视频信息获取 - 使用 Camoufox 反检测浏览器
用法: python get_video_info.py <video_id> [video_id2 ...]
输出: JSON 数组，每个元素包含 playUrl, desc, author, duration, hashtags, createTime, subtitleUrl
"""

import sys
import json
import os

http_proxy = os.environ.get('http_proxy', '') or os.environ.get('HTTP_PROXY', '')
https_proxy = os.environ.get('https_proxy', '') or os.environ.get('HTTPS_PROXY', '')

from camoufox.sync_api import Camoufox

def get_video_info(video_id):
    """用 Camoufox 获取单个视频的 CDN 信息 + 字幕信息"""
    with Camoufox(headless=True) as browser:
        context = browser.new_context()
        page = context.new_page()

        video_data = None

        def handle_response(response):
            nonlocal video_data
            try:
                if f'/aweme/v1/web/aweme/detail/' in response.url and response.status == 200:
                    data = response.json()
                    aweme = data.get('aweme_detail', {})
                    video_info = aweme.get('video', {})
                    play_addr = video_info.get('play_addr', {})
                    url_list = play_addr.get('url_list', [])
                    play_url = ''
                    for u in url_list:
                        if 'vod.com' in u or '.mp4' in u:
                            play_url = u
                            break
                    if not play_url and url_list:
                        play_url = url_list[0]

                    text_extra = aweme.get('text_extra', [])
                    hashtags = [t.get('hashtag_name', '') for t in text_extra if t.get('hashtag_name')]

                    # 提取字幕信息
                    subtitle_url = None
                    subtitle_infos = []
                    # 方式1: video.subtitle 字段
                    subtitles = video_info.get('subtitle', {})
                    subtitle_list = subtitles.get('subtitle_list', [])
                    if subtitle_list:
                        for sub in subtitle_list:
                            sub_url = sub.get('url', '') or sub.get('base_url', '')
                            if sub_url:
                                subtitle_url = sub_url
                                subtitle_infos.append({
                                    'url': sub_url,
                                    'lang': sub.get('lang', 'zh'),
                                    'type': sub.get('format', '') or 'unknown'
                                })
                    # 方式2: ai_dynamic_cover 或 caption 相关
                    if not subtitle_url:
                        captions = aweme.get('caption_info', {}) or aweme.get('ai_caption_info', {})
                        cap_url = captions.get('url', '') or captions.get('base_url', '')
                        if cap_url:
                            subtitle_url = cap_url
                            subtitle_infos.append({'url': cap_url, 'lang': 'zh', 'type': 'caption'})

                    video_data = {
                        'playUrl': play_url,
                        'desc': aweme.get('desc', ''),
                        'author': (aweme.get('author') or {}).get('nickname', ''),
                        'duration': round(video_info.get('duration', 0) / 1000),
                        'hashtags': hashtags,
                        'createTime': aweme.get('create_time', 0),
                        'subtitleUrl': subtitle_url,
                        'subtitleInfos': subtitle_infos,
                        'rawSubtitleList': subtitle_list if subtitle_list else None
                    }
            except Exception:
                pass

        page.on('response', handle_response)

        try:
            page.goto(f'https://www.douyin.com/video/{video_id}', wait_until='domcontentloaded', timeout=30000)
            page.wait_for_timeout(5000)
        except Exception as e:
            return {'error': str(e), 'videoId': video_id}

        if video_data:
            video_data['videoId'] = video_id
            return video_data
        else:
            return {'error': 'No CDN URL from API response', 'videoId': video_id}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: python get_video_info.py <video_id> [video_id2 ...]'}))
        sys.exit(1)

    video_ids = sys.argv[1:]
    results = []

    for i, video_id in enumerate(video_ids):
        print(f'[Camoufox] 获取视频 {i+1}/{len(video_ids)}: {video_id}...', file=sys.stderr)
        result = get_video_info(video_id)
        results.append(result)
        if result.get('playUrl'):
            desc = result['desc'][:30]
            sub = '📝有字幕' if result.get('subtitleUrl') else '📝无字幕'
            print(f'  ✓ {desc} ({result["duration"]}s) {sub}', file=sys.stderr)
        else:
            print(f'  ❌ {result.get("error", "unknown")}', file=sys.stderr)

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
