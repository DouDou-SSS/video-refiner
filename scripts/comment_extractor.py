#!/usr/bin/env python3
"""使用 OpenCLI 提取视频评论"""

import subprocess
import json
import sys

def extract_bilibili_comments(bvid, limit=50):
    """提取 B站视频评论"""
    try:
        result = subprocess.run(
            ['opencli', 'bilibili', 'comments', bvid, '--limit', str(limit), '--format', 'json'],
            capture_output=True, text=True, timeout=30
        )
        json_start = result.stdout.find('[')
        if json_start >= 0:
            return json.loads(result.stdout[json_start:])
        return []
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return []

def extract_douyin_comments(sec_uid, limit=10):
    """提取抖音博主视频评论（通过 user-videos 获取）"""
    try:
        result = subprocess.run(
            ['opencli', 'douyin', 'user-videos', sec_uid, '--limit', str(limit), '--format', 'json'],
            capture_output=True, text=True, timeout=30
        )
        json_start = result.stdout.find('[')
        if json_start >= 0:
            data = json.loads(result.stdout[json_start:])
            # 提取每个视频的评论
            all_comments = []
            for v in data:
                video_comments = v.get('top_comments', [])
                for c in video_comments:
                    c['aweme_id'] = v.get('aweme_id', '')
                    c['video_title'] = v.get('title', '')
                all_comments.extend(video_comments)
            return all_comments
        return []
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return []

def detect_fake_comments(comments, threshold=0.3):
    """检测可能的虚假评论（水军）
    
    Args:
        comments: 评论列表
        threshold: 水军判定阈值
    
    Returns:
        list: 标记后的评论列表，包含 is_suspicious 字段
    """
    spam_patterns = [
        '已跑通', '可教', '有教程', '带', '已跑通，可教',
        '求教', '怎么做', '怎么弄', '求带'
    ]
    
    results = []
    for c in comments:
        text = c.get('text', '')
        likes = c.get('likes', c.get('digg_count', 0))
        
        is_suspicious = False
        reasons = []
        
        # 检查引流关键词
        for pattern in spam_patterns:
            if pattern in text:
                is_suspicious = True
                reasons.append(f'包含引流词: {pattern}')
                break
        
        # 检查无意义短评
        if len(text) < 5 and likes == 0:
            is_suspicious = True
            reasons.append('无意义短评')
        
        # 检查纯表情
        if text and all(ord(ch) > 0x1F000 or ch in '[]_' for ch in text):
            is_suspicious = True
            reasons.append('纯表情评论')
        
        c['is_suspicious'] = is_suspicious
        c['suspicious_reasons'] = reasons
        results.append(c)
    
    return results

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python comment_extractor.py <platform> <id> [--limit N]")
        print("  platform: bilibili | douyin")
        print("  id: bvid (B站) 或 sec_uid (抖音)")
        sys.exit(1)
    
    platform = sys.argv[1]
    vid_id = sys.argv[2]
    limit = 50
    
    # 解析 --limit 参数
    for i, arg in enumerate(sys.argv):
        if arg == '--limit' and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
    
    if platform == 'bilibili':
        comments = extract_bilibili_comments(vid_id, limit)
    elif platform == 'douyin':
        comments = extract_douyin_comments(vid_id, limit)
    else:
        print(f"不支持的平台: {platform}")
        sys.exit(1)
    
    # 标记可疑评论
    comments = detect_fake_comments(comments)
    
    # 输出
    print(json.dumps(comments, ensure_ascii=False, indent=2))
