#!/usr/bin/env python3
"""
智能下载路由器 - 按平台自动选择最优下载方式

用法:
  python download_router.py <视频URL> [--output <输出路径>] [--opencli]

平台路由:
  - douyin.com → MCP 无水印下载 或 OpenCLI CDN 直链
  - bilibili.com → yt-dlp 下载 或 OpenCLI 下载
  - 其他 → Camoufox 浏览器直接下载
"""

import sys
import os
import re
import json
import subprocess


def detect_platform(url):
    """检测视频平台"""
    if 'douyin.com' in url:
        return 'douyin'
    elif 'bilibili.com' in url or 'b23.tv' in url:
        return 'bilibili'
    else:
        return 'other'


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


def check_mcp_available():
    """检查 MCP (mcporter) 是否可用"""
    try:
        result = subprocess.run(
            ['mcporter', 'status'],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except:
        return False


def download_douyin_opencli(url, output_path):
    """抖音 → OpenCLI CDN 直链下载"""
    try:
        # 提取 aweme_id
        aweme_id_match = re.search(r'/video/(\d+)', url)
        if not aweme_id_match:
            return {'error': '无法提取 aweme_id', 'method': 'opencli'}
        
        aweme_id = aweme_id_match.group(1)
        
        # 获取 CDN 直链
        result = subprocess.run(
            ['opencli', 'douyin', 'user-videos', 'temp', '--limit', '1', '--format', 'json'],
            capture_output=True, text=True, timeout=30
        )
        
        # 通过 MCP 获取更准确的信息
        mcp_result = subprocess.run(
            ['mcporter', 'call', 'douyin.get_douyin_download_link', f'{{"aweme_id": "{aweme_id}"}}'],
            capture_output=True, text=True, timeout=30
        )
        
        # 如果 MCP 可用，优先用 MCP
        if mcp_result.returncode == 0:
            try:
                mcp_data = json.loads(mcp_result.stdout)
                download_url = mcp_data.get('download_url', '')
                if download_url:
                    print(f'  🟢 [OpenCLI+MCP] 抖音无水印下载...', file=sys.stderr)
                    proc = subprocess.run(
                        ['curl', '-L', '-o', output_path, download_url],
                        capture_output=True, timeout=300
                    )
                    if proc.returncode == 0 and os.path.getsize(output_path) > 1024:
                        size_mb = os.path.getsize(output_path) / (1024 * 1024)
                        return {
                            'video_id': aweme_id,
                            'download_url': download_url,
                            'output': output_path,
                            'download_size_mb': round(size_mb, 2),
                            'platform': 'douyin',
                            'method': 'opencli+mcp'
                        }
            except:
                pass
        
        # 如果 MCP 不可用，尝试 OpenCLI user-videos
        # 这里需要知道 sec_uid，简化处理返回错误
        return {'error': 'OpenCLI 抖音下载需要 sec_uid，建议使用 MCP', 'method': 'opencli'}
        
    except Exception as e:
        return {'error': str(e), 'method': 'opencli'}


def download_douyin_mcp(url, output_path):
    """抖音 → MCP 无水印下载"""
    try:
        aweme_id_match = re.search(r'/video/(\d+)', url)
        if not aweme_id_match:
            return {'error': '无法提取 aweme_id', 'method': 'mcp'}
        
        aweme_id = aweme_id_match.group(1)
        
        # 获取下载链接
        result = subprocess.run(
            ['mcporter', 'call', 'douyin.get_douyin_download_link',
             json.dumps({"aweme_id": aweme_id})],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            return {'error': result.stderr, 'method': 'mcp'}
        
        data = json.loads(result.stdout)
        download_url = data.get('download_url', '')
        
        if not download_url:
            return {'error': '未获取到下载链接', 'method': 'mcp'}
        
        # 下载视频
        print(f'  🟢 [MCP] 抖音无水印下载...', file=sys.stderr)
        proc = subprocess.run(
            ['curl', '-L', '-o', output_path, download_url],
            capture_output=True, timeout=300
        )
        
        if proc.returncode == 0 and os.path.getsize(output_path) > 1024:
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            return {
                'video_id': aweme_id,
                'title': data.get('title', ''),
                'download_url': download_url,
                'output': output_path,
                'status': 'success',
                'download_size_mb': round(size_mb, 2),
                'platform': 'douyin',
                'method': 'mcp'
            }
        else:
            return {'error': '下载失败或文件太小', 'method': 'mcp'}
            
    except Exception as e:
        return {'error': str(e), 'method': 'mcp'}


def download_bilibili_opencli(url, output_path):
    """B站 → OpenCLI 下载"""
    try:
        # 提取 BV ID
        bvid_match = re.search(r'(BV[A-Za-z0-9]{10})', url)
        if not bvid_match:
            return {'error': '无法提取 BV ID', 'method': 'opencli'}
        
        bvid = bvid_match.group(1)
        
        # 使用 opencli download
        result = subprocess.run(
            ['opencli', 'bilibili', 'download', bvid],
            capture_output=True, text=True, timeout=300,
            cwd=os.path.dirname(output_path) or '.'
        )
        
        if result.returncode == 0:
            # 查找下载的文件
            if os.path.exists(output_path):
                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                return {
                    'video_id': bvid,
                    'output': output_path,
                    'download_size_mb': round(size_mb, 2),
                    'platform': 'bilibili',
                    'method': 'opencli'
                }
        
        # 如果直接下载失败，尝试 yt-dlp
        return download_bilibili_ytdlp(url, output_path)
        
    except Exception as e:
        return {'error': str(e), 'method': 'opencli'}


def download_bilibili_ytdlp(url, output_path):
    """B站 → yt-dlp 下载"""
    try:
        bvid_match = re.search(r'(BV[A-Za-z0-9]{10})', url)
        if not bvid_match:
            return {'error': '无法提取 BV ID', 'method': 'yt-dlp'}
        
        bvid = bvid_match.group(1)
        proxy = os.environ.get('http_proxy', '') or os.environ.get('https_proxy', '')
        
        cmd = [
            'yt-dlp',
            '--cookies-from-browser', 'chrome',
            '--no-warnings',
            '-o', output_path,
            url
        ]
        if proxy:
            cmd.insert(2, '--proxy')
            cmd.insert(3, proxy)
        
        print(f'  🟡 [yt-dlp] B站视频下载...', file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        
        if os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            return {
                'platform': 'bilibili',
                'method': 'yt-dlp',
                'video_id': bvid,
                'output': output_path,
                'download_size_mb': round(size_mb, 2)
            }
        else:
            return {'error': 'yt-dlp 下载失败', 'method': 'yt-dlp', 'stderr': result.stderr}
            
    except Exception as e:
        return {'error': str(e), 'method': 'yt-dlp'}


def download_other_camoufox(url, output_path):
    """其他平台 → Camoufox 下载（保留原有逻辑）"""
    try:
        from camoufox.sync_api import Camoufox
        
        with Camoufox(headless=True) as browser:
            context = browser.new_context()
            page = context.new_page()
            
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            
            # 尝试获取视频 URL
            video_url = page.evaluate('''() => {
                const video = document.querySelector('video');
                return video ? video.src : null;
            }''')
            
            if video_url:
                subprocess.run(['curl', '-L', '-o', output_path, video_url], timeout=300)
                if os.path.exists(output_path):
                    size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    return {
                        'platform': 'other',
                        'method': 'camoufox',
                        'download_size_mb': round(size_mb, 2)
                    }
        
        return {'error': 'Camoufox 未能获取视频 URL', 'method': 'camoufox'}
        
    except Exception as e:
        return {'error': str(e), 'method': 'camoufox'}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': '用法: python download_router.py <视频URL> [--output <路径>]'}))
        sys.exit(1)
    
    url = sys.argv[1]
    platform = detect_platform(url)
    
    # 解析输出路径
    output_path = None
    for i, arg in enumerate(sys.argv):
        if arg == '--output' and i + 1 < len(sys.argv):
            output_path = sys.argv[i + 1]
    
    if not output_path:
        # 默认输出路径
        video_id_match = re.search(r'(BV[A-Za-z0-9]{10}|\d{15,})', url)
        video_id = video_id_match.group(1) if video_id_match else 'video'
        output_path = f'{platform}_{video_id}.mp4'
    
    print(f'🎯 平台: {platform} | URL: {url[:60]}...', file=sys.stderr)
    
    # 检查 OpenCLI 可用性
    opencli_available = check_opencli_available()
    
    if platform == 'douyin':
        # 抖音：优先 MCP，OpenCLI 需要 sec_uid
        if check_mcp_available():
            result = download_douyin_mcp(url, output_path)
        elif opencli_available:
            result = download_douyin_opencli(url, output_path)
        else:
            result = {'error': '抖音下载需要 MCP 或 OpenCLI', 'method': 'none'}
    
    elif platform == 'bilibili':
        # B站：优先 OpenCLI（如果 Chrome 已开），否则 yt-dlp
        if opencli_available:
            print(f'  🟢 [OpenCLI] B站视频下载...', file=sys.stderr)
            result = download_bilibili_opencli(url, output_path)
        else:
            result = download_bilibili_ytdlp(url, output_path)
    
    else:
        # 其他平台
        result = download_other_camoufox(url, output_path)
    
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
