#!/usr/bin/env python3
"""
批量将本地知识库写入Obsidian，自动分类
"""
import os
import sys
import json
from pathlib import Path

# 配置
LOCAL_KB = Path("~/VideoAutomation/知识库")
OBSIDIAN_VAULT = Path(os.path.expanduser("~/Obsidian-Vault/知识库"))

# 需要排除的文件
EXCLUDE_FILES = {'knowledge_extract.py', 'cross_validate.py', '知识提炼.md'}

def read_openclaw_config():
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        bailian = config.get('models', {}).get('providers', {}).get('bailian', {})
        api_key = bailian.get('apiKey', '')
        return api_key
    except:
        return os.environ.get('DASHSCOPE_API_KEY', '')

def classify(title, content):
    """根据标题和内容判断分类"""
    from openai import OpenAI

    api_key = read_openclaw_config()
    client = OpenAI(api_key=api_key, base_url='https://coding.dashscope.aliyuncs.com/v1')

    existing_dirs = [d.name for d in OBSIDIAN_VAULT.iterdir() if d.is_dir() and not d.name.startswith('.')]

    prompt = f"""你是一个知识分类专家。请根据以下内容判断分类目录名。

已有分类目录：{', '.join(existing_dirs) if existing_dirs else '（暂无）'}

视频标题：{title}

知识提炼摘要（前1000字）：
{content[:1000]}

要求：
1. 如果内容适合已有分类，直接返回分类目录名（必须完全一致）
2. 如果不适合任何已有分类，创建新的分类目录名（用中文，简洁）
3. 只返回分类目录名，不要其他内容"""

    resp = client.chat.completions.create(
        model="qwen3.6-plus",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=50,
        temperature=0.1,
    )
    category = resp.choices[0].message.content.strip().strip('"\'`').replace('/', '-')

    if len(category) > 30:
        category = category[:30]
    return category

def main():
    # 收集本地知识库中的知识提炼.md文件
    knowledge_files = []
    for item in LOCAL_KB.iterdir():
        if not item.is_dir():
            continue
        if item.name in EXCLUDE_FILES:
            continue
        knowledge_file = item / "知识提炼.md"
        if knowledge_file.exists():
            knowledge_files.append((item.name, knowledge_file))

    print(f"📋 找到 {len(knowledge_files)} 个知识提炼文件")

    # 统计
    categories = {}

    # 处理每个文件
    for name, source_file in knowledge_files:
        print(f"\n处理: {name}")

        with open(source_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 提取标题（从文件内容第一行）
        title_line = content.split('\n')[0]
        title = title_line.replace('#', '').strip() or name

        # 分类
        try:
            category = classify(title, content)
        except Exception as e:
            print(f"   ⚠️ 分类失败: {e}，使用默认分类")
            category = "未分类"

        # 创建目标目录
        target_dir = OBSIDIAN_VAULT / category
        target_dir.mkdir(parents=True, exist_ok=True)

        # 目标文件
        file_name = name.replace('_BV', '').replace('_dy_', '').replace('七国', '')[:50].strip()
        target_file = target_dir / f"{file_name}.md"

        # 写入
        with open(target_file, 'w', encoding='utf-8') as f:
            f.write(content)

        print(f"   ✅ → {target_file}")

        # 统计
        if category not in categories:
            categories[category] = []
        categories[category].append(file_name)

    # 输出统计
    print(f"\n📊 统计：")
    for cat, files in sorted(categories.items()):
        print(f"  {cat}: {len(files)} 个文件")

    print(f"\n✅ 完成！已写入 {len(knowledge_files)} 个文件到 Obsidian")

if __name__ == '__main__':
    main()
