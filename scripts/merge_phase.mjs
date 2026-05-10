#!/usr/bin/env node
/**
 * 合并精炼阶段 — 用已有单视频分析生成最终 5 份 .md 产品
 */
import { readFileSync, writeFileSync, readdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

const DIMENSIONS = [
  { key: '文案风格', file: '文案风格.md', promptFile: '文案风格蒸馏.md' },
  { key: '视频脚本', file: '视频脚本.md', promptFile: '视频脚本蒸馏.md' },
  { key: '剪辑逻辑', file: '剪辑逻辑.md', promptFile: '剪辑逻辑蒸馏.md' },
  { key: '选题策略', file: '选题策略.md', promptFile: '选题策略蒸馏.md' },
  { key: '运营策略', file: '运营策略.md', promptFile: '运营策略蒸馏.md' },
];

const OUTPUT_DIR = process.argv[2];
if (!OUTPUT_DIR) {
  console.error('用法: node merge_phase.mjs <输出目录>');
  process.exit(1);
}
const SINGLE_DIR = join(OUTPUT_DIR, '单视频分析');

console.log('=== 阶段三：合并精炼 ===');
console.log(`输出目录: ${OUTPUT_DIR}`);

// 读取 API Key（从环境变量或 openclaw.json）
let apiKey = process.env.DASHSCOPE_API_KEY;
if (!apiKey) {
  const homeDir = process.env.HOME || process.env.USERPROFILE;
  const configPath = join(homeDir, '.openclaw', 'openclaw.json');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  apiKey = config.models?.providers?.bailian?.apiKey;
}
if (!apiKey) {
  console.error('❌ 未找到百炼 API Key');
  process.exit(1);
}

// 构建 OpenAI 兼容客户端
import OpenAI from 'openai';
const openai = new OpenAI({
  apiKey,
  baseURL: 'https://coding.dashscope.aliyuncs.com/v1',
});

// 获取所有视频 ID
const files = readdirSync(SINGLE_DIR).filter(f => f.endsWith('.md'));
const videoIds = [...new Set(files.map(f => f.split('_')[0]))];
console.log(`发现 ${videoIds.length} 个视频的分析结果`);

for (const dim of DIMENSIONS) {
  console.log(`\n📝 合并: ${dim.key}`);
  
  // 收集该维度所有单视频分析
  const analyses = [];
  for (const vid of videoIds) {
    const filePath = join(SINGLE_DIR, `${vid}_${dim.key}.md`);
    try {
      const content = readFileSync(filePath, 'utf8');
      analyses.push({ videoId: vid, content });
      console.log(`  ✓ 视频 ${vid} (${content.length} 字)`);
    } catch (e) {
      console.log(`  ⚠ 视频 ${vid} 缺失`);
    }
  }

  if (analyses.length === 0) {
    console.log(`  ⏭️ 跳过（无数据）`);
    continue;
  }

  // 读取提示词
  const promptPath = join(__dirname, '..', 'prompts', dim.promptFile);
  const prompt = readFileSync(promptPath, 'utf8');

  // 构建合并请求
  const mergedContent = analyses.map(a =>
    `## 视频 ${a.videoId}\n\n${a.content}`
  ).join('\n\n---\n\n');

  const systemPrompt = `你是资深内容分析师。请汇总以下 ${analyses.length} 个视频的单维度分析结果，提炼共性规律、公式、模板。
要求：
1. 找出所有视频的共同模式和独特之处
2. 提炼可复用的方法论和公式
3. 给出具体示例和引用
4. 结构清晰，层次分明
5. 输出完整的 Markdown 文档`;

  const userMessage = `${prompt}\n\n以下是 ${analyses.length} 个视频的单视频分析结果，请合并精炼：\n\n${mergedContent}`;

  try {
    const response = await openai.chat.completions.create({
      model: 'qwen3.6-plus',
      messages: [
        { role: 'system', content: systemPrompt },
        { role: 'user', content: userMessage },
      ],
      temperature: 0.7,
      max_tokens: 8000,
    });

    const result = response.choices[0].message.content;
    const outPath = join(OUTPUT_DIR, dim.file);
    writeFileSync(outPath, result, 'utf8');
    console.log(`  ✅ ${dim.file} (${result.length} 字)`);
    console.log(`  📁 ${outPath}`);
  } catch (e) {
    console.error(`  ❌ ${dim.key} 失败: ${e.message}`);
    // 尝试备用模型
    try {
      console.log(`  🔄 切换备用模型 deepseek-v4...`);
      const response = await openai.chat.completions.create({
        model: 'deepseek-v4',
        messages: [
          { role: 'system', content: systemPrompt },
          { role: 'user', content: userMessage },
        ],
        temperature: 0.7,
        max_tokens: 8000,
      });
      const result = response.choices[0].message.content;
      const outPath = join(OUTPUT_DIR, dim.file);
      writeFileSync(outPath, result, 'utf8');
      console.log(`  ✅ ${dim.file} (${result.length} 字)`);
    } catch (e2) {
      console.error(`  ❌ 备用模型也失败: ${e2.message}`);
    }
  }

  // 维度间延迟
  if (DIMENSIONS.indexOf(dim) < DIMENSIONS.length - 1) {
    const delay = Math.floor(Math.random() * 10000) + 10000;
    console.log(`  ⏳ 等待 ${(delay / 1000).toFixed(1)} 秒...`);
    await new Promise(r => setTimeout(r, delay));
  }
}

console.log('\n=== 合并精炼完成 ===');
console.log('最终产品:');
for (const dim of DIMENSIONS) {
  const outPath = join(OUTPUT_DIR, dim.file);
  try {
    const stat = require('fs').statSync(outPath);
    console.log(`  ✅ ${dim.file} (${stat.size} bytes)`);
  } catch (e) {
    console.log(`  ❌ ${dim.file} 未生成`);
  }
}
