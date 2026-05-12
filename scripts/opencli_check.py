#!/usr/bin/env python3
"""检查 OpenCLI 是否可用（Chrome 已打开且扩展已连接）"""

import subprocess
import json
import os

def check_opencli():
    """检查 OpenCLI 是否可用
    
    Returns:
        tuple: (available: bool, message: str)
    """
    try:
        # 检查 opencli 命令是否存在
        result = subprocess.run(
            ['which', 'opencli'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return False, "opencli 未安装"
        
        # 检查 daemon 和扩展连接状态
        result = subprocess.run(
            ['opencli', 'doctor'],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        
        # 检查关键状态
        extension_ok = 'Extension: connected' in output or '[OK] Extension:' in output
        connectivity_ok = 'Connectivity: connected' in output or '[OK] Connectivity:' in output
        
        if extension_ok and connectivity_ok:
            return True, "OpenCLI 可用（Chrome 已连接）"
        else:
            # 提取具体错误信息
            for line in output.split('\n'):
                if 'FAIL' in line or 'MISSING' in line or 'error' in line.lower():
                    return False, f"OpenCLI 不可用: {line.strip()}"
            return False, "OpenCLI Chrome 扩展未连接"
            
    except subprocess.TimeoutExpired:
        return False, "opencli doctor 超时"
    except Exception as e:
        return False, f"检查失败: {str(e)}"

def get_opencli_platforms():
    """获取 OpenCLI 支持的平台列表"""
    try:
        result = subprocess.run(
            ['opencli', 'list'],
            capture_output=True, text=True, timeout=10
        )
        # 解析支持的平台
        platforms = []
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line and not line.startswith('opencli') and ':' in line:
                platform = line.split()[0]
                platforms.append(platform)
        return platforms
    except:
        return []

if __name__ == '__main__':
    available, msg = check_opencli()
    status = "✅" if available else "❌"
    print(f"{status} {msg}")
    
    if available:
        platforms = get_opencli_platforms()
        if platforms:
            print(f"   支持平台: {', '.join(platforms[:10])}...")
