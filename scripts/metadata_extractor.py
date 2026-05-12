#!/usr/bin/env python3
"""使用 OpenCLI 提取视频元数据"""

import subprocess
import json
import sys

def extract_bilibili_metadata(bvid):
    """提取 B站视频元数据"""
    try:
        result = subprocess.run(
            ['opencli', 'bilibili', 'video', bvid, '--format', 'json'],
            capture_output=True, text=True, timeout=30
        )
        # 过滤掉扩展更新提示等非 JSON 输出
        json_start = result.stdout.find('[')
        if json_start >= 0:
            data = json.loads(result.stdout[json_start:])
            return {k: v for item in data for k, v in [(item['field'], item['value'])]}
        return None
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return None

def extract_douyin_metadata(aweme_id):
    """提取抖音视频元数据（从 user-videos 结果中查找）"""
    # 抖音需要通过 user-videos 获取，这里简化处理
    pass

def batch_extract_bilibili(bvids, output_file=None):
    """批量提取 B站视频元数据"""
    results = []
    for bvid in bvids:
        print(f"  提取 {bvid}...", file=sys.stderr)
        meta = extract_bilibili_metadata(bvid)
        if meta:
            results.append(meta)
    
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    
    return results

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python metadata_extractor.py <bvid> [--batch <file>]")
        sys.exit(1)
    
    if sys.argv[1] == '--batch':
        with open(sys.argv[2], 'r') as f:
            bvids = [line.strip() for line in f if line.strip()]
        results = batch_extract_bilibili(bvids)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        bvid = sys.argv[1]
        meta = extract_bilibili_metadata(bvid)
        if meta:
            print(json.dumps(meta, ensure_ascii=False, indent=2))
