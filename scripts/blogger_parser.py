#!/usr/bin/env python3
"""
博主主页视频解析器 - 使用 OpenCLI（首选）或 Camoufox（降级）

用法:
  python blogger_parser.py <博主主页URL>

输出: JSON，包含 video_ids 列表和 count

解析策略:
  - 抖音博主 → OpenCLI（含 aweme_id、标题、时长、点赞、CDN直链、评论）
  - B站博主 → OpenCLI（含 bvid、标题、播放量、点赞数）
  - 降级方案 → 原有 Camoufox/yt-dlp 方案
"""

import sys
import json
import os
import re
import subprocess

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
            return 'blogger'
    else:
        if re.match(r'^\d{15,}$', url_or_id):
            return 'douyin_id'
        elif re.match(r'^BV[A-Za-z0-9]{10}$', url_or_id):
            return 'bilibili_id'
        return 'unknown'


# ========================
# OpenCLI 相关
# ========================
def check_opencli_available():
    """检查 OpenCLI 是否可用"""
    try:
        result = subprocess.run(
            ['opencli', 'doctor'],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        ext_ok = 'Extension: connected' in output or '[OK] Extension:' in output
        conn_ok = 'Connectivity: connected' in output or '[OK] Connectivity:' in output
        return ext_ok and conn_ok
    except:
        return False


def parse_douyin_blogger_opencli(url, limit=50):
    """抖音博主 → OpenCLI 解析（首选）"""
    # 提取 sec_uid
    sec_uid_match = re.search(r'/user/([^?&]+)', url)
    if not sec_uid_match:
        return None, "无法提取 sec_uid"
    
    sec_uid = sec_uid_match.group(1)
    
    try:
        result = subprocess.run(
            ['opencli', 'douyin', 'user-videos', sec_uid, '--limit', str(limit), '--format', 'json'],
            capture_output=True, text=True, timeout=60
        )
        
        # 过滤非 JSON 输出
        json_start = result.stdout.find('[')
        if json_start < 0:
            return None, f"OpenCLI 返回异常: {result.stderr[:200]}"
        
        data = json.loads(result.stdout[json_start:])
        
        video_ids = []
        for v in data:
            video_ids.append({
                'video_id': v.get('aweme_id', ''),
                'title': v.get('title', ''),
                'duration': v.get('duration', 0),
                'digg_count': v.get('digg_count', 0),
                'play_url': v.get('play_url', ''),
                'top_comments': v.get('top_comments', [])
            })
        
        return {'video_ids': video_ids, 'count': len(video_ids), 'method': 'opencli'}, None
        
    except subprocess.TimeoutExpired:
        return None, "OpenCLI 超时"
    except Exception as e:
        return None, f"OpenCLI 异常: {str(e)}"


def parse_bilibili_blogger_opencli(url, limit=50):
    """B站博主 → OpenCLI 解析（首选）"""
    # 提取 UID
    uid_match = re.search(r'space\.bilibili\.com/(\d+)', url)
    if not uid_match:
        return None, "无法提取 UID"
    
    uid = uid_match.group(1)
    
    try:
        result = subprocess.run(
            ['opencli', 'bilibili', 'user-videos', uid, '--limit', str(limit), '--format', 'json'],
            capture_output=True, text=True, timeout=60
        )
        
        json_start = result.stdout.find('[')
        if json_start < 0:
            return None, f"OpenCLI 返回异常: {result.stderr[:200]}"
        
        data = json.loads(result.stdout[json_start:])
        
        video_ids = []
        for v in data:
            bvid = v.get('url', '').split('/')[-1] if v.get('url') else ''
            video_ids.append({
                'video_id': bvid,
                'title': v.get('title', ''),
                'plays': v.get('plays', 0),
                'likes': v.get('likes', 0),
                'date': v.get('date', ''),
                'url': v.get('url', '')
            })
        
        return {'video_ids': video_ids, 'count': len(video_ids), 'method': 'opencli'}, None
        
    except subprocess.TimeoutExpired:
        return None, "OpenCLI 超时"
    except Exception as e:
        return None, f"OpenCLI 异常: {str(e)}"


# ========================
# 原有 Camoufox 方案（降级）
# ========================
def parse_douyin_blogger_camoufox(url):
    """抖音博主 → Camoufox + API 监听（降级方案）"""
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
            for _ in range(5):
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(3000)
        except Exception as e:
            return {'error': str(e)}

    return {'video_ids': video_ids, 'count': len(video_ids), 'method': 'camoufox'}


def parse_bilibili_blogger_ytdlp(url):
    """B站博主 → yt-dlp（降级方案）"""
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
        return {'video_ids': lines, 'count': len(lines), 'method': 'yt-dlp'}
    except subprocess.TimeoutExpired:
        return {'error': 'yt-dlp 超时'}
    except Exception as e:
        return {'error': f'yt-dlp 异常: {str(e)}'}


def parse_bilibili_blogger_camoufox(url):
    """B站博主 → Camoufox（降级方案的降级）"""
    video_ids = []
    uid_match = re.search(r'space\.bilibili\.com/(\d+)', url)
    if not uid_match:
        return {'error': '无法提取 UID'}

    uid = uid_match.group(1)

    with Camoufox(headless=True) as browser:
        context = browser.new_context()
        page = context.new_page()

        def handle_response(response):
            nonlocal video_ids
            try:
                url_resp = response.url
                if 'api.bilibili.com' in url_resp and 'arc/search' in url_resp and response.status == 200:
                    data = response.json()
                    vlist = data.get('data', {}).get('list', {}).get('vlist', [])
                    for v in vlist:
                        bvid = v.get('bvid', '')
                        if bvid and bvid not in video_ids:
                            video_ids.append(bvid)
            except Exception:
                pass

        page.on('response', handle_response)

        try:
            page.goto(url, wait_until='networkidle', timeout=30000)
            for _ in range(3):
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(3000)
        except Exception as e:
            return {'error': str(e)}

    if video_ids:
        return {'video_ids': video_ids, 'count': len(video_ids), 'method': 'camoufox'}
    else:
        return {'error': 'Camoufox 未捕获到 B 站视频列表'}


def parse_generic_blogger(url):
    """其他平台博主 → Camoufox 通用解析"""
    video_ids = []
    video_urls = []

    with Camoufox(headless=True) as browser:
        context = browser.new_context()
        page = context.new_page()

        def handle_response(response):
            nonlocal video_ids
            try:
                if response.status == 200:
                    content_type = response.headers.get('content-type', '')
                    if 'application/json' in content_type:
                        data = response.json()
                        _extract_ids_recursive(data, video_ids)
            except Exception:
                pass

        page.on('response', handle_response)

        try:
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            for _ in range(5):
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(3000)

            extracted = page.evaluate('''() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                const urls = [];
                const ids = [];
                for (const link of links) {
                    const href = link.href || '';
                    const douyinMatch = href.match(/(\\d{15,})/);
                    if (douyinMatch && !href.includes('space')) {
                        ids.push(douyinMatch[1]);
                    }
                    const bvMatch = href.match(/(BV[A-Za-z0-9]{10})/);
                    if (bvMatch) { ids.push(bvMatch[1]); }
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

    if video_ids:
        return {'video_ids': video_ids, 'count': len(video_ids), 'method': 'camoufox'}
    elif video_urls:
        return {'video_urls': video_urls, 'count': len(video_urls), 'method': 'camoufox'}
    else:
        return {'error': '未能从页面提取到视频 ID 或链接'}


def _extract_ids_recursive(data, ids_list):
    """递归从 JSON 数据结构中提取视频 ID"""
    if isinstance(data, dict):
        for key in ['aweme_id', 'video_id', 'vid', 'bvid', 'aid', 'id']:
            val = data.get(key)
            if val and isinstance(val, str) and len(val) >= 10:
                if val not in ids_list:
                    ids_list.append(val)
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
        print(json.dumps({'error': '用法: python blogger_parser.py <URL> 或 <视频ID>'}))
        sys.exit(1)

    url_or_id = sys.argv[1]
    input_type = detect_input_type(url_or_id)
    opencli_available = check_opencli_available()

    print(f'[解析器] 输入类型: {input_type}', file=sys.stderr)
    print(f'[解析器] 输入: {url_or_id[:80]}...', file=sys.stderr)
    print(f'[解析器] OpenCLI: {"可用" if opencli_available else "不可用"}', file=sys.stderr)

    if input_type == 'blogger':
        platform = detect_platform(url_or_id)
        print(f'[解析器] 平台: {platform}', file=sys.stderr)

        if platform == 'douyin':
            if opencli_available:
                result, err = parse_douyin_blogger_opencli(url_or_id)
                if result:
                    print(f'[解析器] 使用 OpenCLI 解析抖音', file=sys.stderr)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return
                print(f'[解析器] OpenCLI 失败: {err}，降级到 Camoufox', file=sys.stderr)
            result = parse_douyin_blogger_camoufox(url_or_id)
            
        elif platform == 'bilibili':
            if opencli_available:
                result, err = parse_bilibili_blogger_opencli(url_or_id)
                if result:
                    print(f'[解析器] 使用 OpenCLI 解析B站', file=sys.stderr)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return
                print(f'[解析器] OpenCLI 失败: {err}，降级到 yt-dlp', file=sys.stderr)
            result = parse_bilibili_blogger_ytdlp(url_or_id)
            if 'error' in result or result.get('count', 0) == 0:
                print(f'[解析器] yt-dlp 失败，降级到 Camoufox', file=sys.stderr)
                result = parse_bilibili_blogger_camoufox(url_or_id)
            
        else:
            print(f'[解析器] 使用 Camoufox 通用解析', file=sys.stderr)
            result = parse_generic_blogger(url_or_id)

    elif input_type in ('single_video', 'douyin_id', 'bilibili_id'):
        result = {'video_ids': [{'video_id': url_or_id}], 'count': 1, 'type': 'single', 'method': 'direct'}

    else:
        result = {'error': f'无法识别输入: {url_or_id}'}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
