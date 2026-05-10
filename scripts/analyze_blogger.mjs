// 视频创作者方法论蒸馏 - 多视频批量分析 v5
// 基于 "同事.skill" 的隐性经验蒸馏方法论
// v5: 常驻Whisper服务 + 蒸馏间隔延迟 + 保留视频不删除
import fs from 'fs';
import path from 'path';
import { execSync, spawn } from 'child_process';
import { OpenAI } from 'openai';

// ========================
// 配置
// ========================
const SKILL_DIR = path.resolve(import.meta.dirname, '..');
const PROMPTS_DIR = path.join(SKILL_DIR, 'prompts');
const SCRIPTS_DIR = path.join(SKILL_DIR, 'scripts');

const CAMOUFOX_PY = path.join(SCRIPTS_DIR, 'get_video_info.py');
const CROSS_VALIDATE_PY = path.join(SCRIPTS_DIR, 'cross_validate.py');
const WHISPER_SERVICE_PY = path.join(SCRIPTS_DIR, 'whisper_service.py');

// Python 选择：系统python 用于 Whisper（模型已缓存，加载快），camoufox-env 用于 Camoufox
const SYSTEM_PYTHON = '/opt/homebrew/bin/python3';
const CAMOUFOX_VENV = path.join(process.env.HOME, 'camoufox-env');
const CAMOUFOX_PYTHON = path.join(CAMOUFOX_VENV, 'bin', 'python3');

// 最终输出模型配置（最强模型 + 思考模式）
const FINAL_MODEL = 'qwen3.6-plus';  // 主模型：最强
const FINAL_MODEL_FALLBACK = 'deepseek-v4';  // 备用：qwen3.6-plus 不可用时
const FINAL_MODEL_THINKING = 'high';  // 思考模式
const ANALYSIS_MODEL = 'qwen3.6-plus';  // 蒸馏分析模型（多模态，主模型）

// 蒸馏维度
const DIMENSIONS = [
  { name: '文案风格', file: '文案风格蒸馏.md', output: '文案风格.md' },
  { name: '视频脚本', file: '视频脚本蒸馏.md', output: '视频脚本.md' },
  { name: '剪辑逻辑', file: '剪辑逻辑蒸馏.md', output: '剪辑逻辑.md' },
  { name: '选题策略', file: '选题策略蒸馏.md', output: '选题策略.md' },
  { name: '运营策略', file: '运营策略蒸馏.md', output: '运营策略.md' },
];

// 防封参数
const ANTI_BAN = {
  minDelay: 3000,
  maxDelay: 8000,
  dailyLimit: 50,
};

// 蒸馏间隔延迟（防止触发云端模型频率限制）
const DISTILL_DELAY = {
  minMs: 10000,  // 10秒
  maxMs: 20000,  // 20秒
};

// ========================
// 初始化
// ========================
const configPath = path.join(process.env.HOME, '.openclaw', 'openclaw.json');
const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
const apiKey = config.models.providers.bailian.apiKey;
const client = new OpenAI({ apiKey, baseURL: 'https://coding.dashscope.aliyuncs.com/v1' });

// ========================
// 参数解析
// ========================
const args = process.argv.slice(2);
let videoIds = [];
let outputDir = null;

for (let i = 0; i < args.length; i++) {
  if (args[i] === '--output' && i + 1 < args.length) {
    outputDir = args[++i];
  } else if (args[i] === '--batch' && i + 1 < args.length) {
    const batchFile = args[++i];
    const links = fs.readFileSync(batchFile, 'utf-8').split('\n').filter(l => l.trim());
    videoIds = links.map(l => {
      const m = l.match(/modal_id=(\d+)/);
      if (m) return m[1];
      const m2 = l.match(/\/video\/(\d+)/);
      if (m2) return m2[1];
      return null;
    }).filter(Boolean);
  } else if (!args[i].startsWith('--')) {
    const m = args[i].match(/modal_id=(\d+)/);
    if (m) videoIds.push(m[1]);
    else if (args[i].match(/\d{15,}/)) videoIds.push(args[i].match(/\d{15,}/)[0]);
    else videoIds.push(args[i]);
  }
}

if (!videoIds.length) {
  console.log('用法: node analyze_blogger.mjs <视频ID或链接> [--output <输出目录>]');
  console.log('   或: node analyze_blogger.mjs --batch <链接文件> [--output <输出目录>]');
  process.exit(1);
}

if (videoIds.length > ANTI_BAN.dailyLimit) {
  console.log(`⚠️ 视频数量 ${videoIds.length} 超过单日上限 ${ANTI_BAN.dailyLimit}，将只处理前 ${ANTI_BAN.dailyLimit} 个`);
  videoIds = videoIds.slice(0, ANTI_BAN.dailyLimit);
}

if (!outputDir) {
  outputDir = path.join(process.env.HOME, 'Desktop', '博主分析_' + new Date().toISOString().slice(0, 10));
}

const TMP_DIR = path.join(outputDir, '原始数据');
const SINGLE_DIR = path.join(outputDir, '单视频分析');
const TRANSCRIPT_DIR = path.join(outputDir, '文案');
const VIDEO_KEEP_DIR = path.join(outputDir, '视频保留');
fs.mkdirSync(TMP_DIR, { recursive: true });
fs.mkdirSync(SINGLE_DIR, { recursive: true });
fs.mkdirSync(TRANSCRIPT_DIR, { recursive: true });
fs.mkdirSync(VIDEO_KEEP_DIR, { recursive: true });

// ========================
// 工具函数
// ========================
function randomDelay(min, max) {
  const ms = Math.floor(Math.random() * (max - min)) + min;
  console.log(`    ⏳ 等待 ${(ms / 1000).toFixed(1)} 秒...`);
  return new Promise(r => setTimeout(r, ms));
}

function getEnv() {
  return {
    ...process.env,
    PATH: process.env.PATH || '',
    http_proxy: process.env.http_proxy || '',
    https_proxy: process.env.https_proxy || '',
  };
}

// ========================
// 常驻 Whisper 服务管理
// ========================
let whisperProc = null;
let whisperBuffer = '';
let whisperResolve = null;
let whisperReject = null;
let whisperTimeout = null;

async function startWhisperService() {
  console.log('    🔄 启动 Whisper 常驻服务...');
  whisperProc = spawn(SYSTEM_PYTHON, [WHISPER_SERVICE_PY], {
    stdio: ['pipe', 'pipe', 'inherit'],
    env: getEnv(),
  });

  whisperProc.stdout.on('data', (data) => {
    whisperBuffer += data.toString();

    // 检查是否有完整的响应
    while (true) {
      const eofIdx = whisperBuffer.indexOf('RESULT_EOF\n');
      if (eofIdx === -1) break;

      const chunk = whisperBuffer.substring(0, eofIdx).trim();
      whisperBuffer = whisperBuffer.substring(eofIdx + 'RESULT_EOF\n'.length);

      if (whisperResolve) {
        clearTimeout(whisperTimeout);
        if (chunk.startsWith('RESULT_OK ')) {
          // RESULT_OK <video_id> <length>\n<text>
          const firstLineEnd = chunk.indexOf('\n');
          const header = chunk.substring(0, firstLineEnd);
          const text = chunk.substring(firstLineEnd + 1);
          whisperResolve({ ok: true, text, header });
        } else if (chunk.startsWith('RESULT_ERR ')) {
          // RESULT_ERR <video_id> <error>
          const err = chunk.substring('RESULT_ERR '.length);
          const spaceIdx = err.indexOf(' ');
          const videoId = err.substring(0, spaceIdx);
          const errMsg = err.substring(spaceIdx + 1);
          whisperResolve({ ok: false, error: errMsg, videoId });
        } else {
          whisperReject(new Error('Unknown response: ' + chunk.substring(0, 100)));
        }
        whisperResolve = null;
        whisperReject = null;
      }
    }
  });

  whisperProc.on('error', (err) => {
    console.log(`    ❌ Whisper 服务启动失败: ${err.message}`);
    if (whisperReject) {
      whisperReject(err);
      whisperResolve = null;
      whisperReject = null;
    }
  });

  // 等待服务就绪
  await new Promise((resolve, reject) => {
    const checkReady = (data) => {
      const text = data.toString();
      whisperBuffer += text;
      if (whisperBuffer.includes('WHISPER_READY')) {
        whisperProc.stdout.removeListener('data', checkReady);
        resolve();
      }
    };
    whisperProc.stdout.on('data', checkReady);
    setTimeout(() => reject(new Error('Whisper 服务启动超时')), 30000);
  });
  whisperBuffer = '';  // 清空就绪信号

  console.log('    ✅ Whisper 常驻服务已就绪（模型按需加载，不再重复启动）');
}

async function whisperTranscribe(videoPath, videoId, modelSize) {
  return new Promise((resolve, reject) => {
    whisperResolve = resolve;
    whisperReject = reject;
    whisperTimeout = setTimeout(() => {
      if (whisperReject) {
        whisperReject(new Error('Whisper 转录超时（30分钟）'));
        whisperResolve = null;
        whisperReject = null;
      }
    }, 30 * 60 * 1000);

    whisperProc.stdin.write(`TRANSCRIBE ${videoPath} ${videoId} ${modelSize}\n`);
  });
}

async function stopWhisperService() {
  if (whisperProc) {
    console.log('    🛑 停止 Whisper 常驻服务...');
    whisperProc.stdin.write('QUIT\n');
    whisperProc.stdin.end();
    await new Promise(r => setTimeout(r, 2000));
    whisperProc.kill();
    whisperProc = null;
    console.log('    ✅ Whisper 服务已停止');
  }
}

// ========================
// Camoufox 获取视频信息
// ========================
async function getVideoInfoCamoufox(videoId) {
  console.log(`    🔵 [Camoufox] 反检测浏览器获取视频信息...`);
  try {
    const result = execSync(`"${CAMOUFOX_PYTHON}" "${CAMOUFOX_PY}" "${videoId}"`, {
      timeout: 60000, stdio: ['pipe', 'pipe', 'inherit'], env: getEnv(),
    }).toString().trim();

    const data = JSON.parse(result);
    if (Array.isArray(data) && data.length > 0) {
      const info = data[0];
      if (info.error) throw new Error(info.error);
      return info;
    }
    throw new Error('Empty response');
  } catch (e) {
    return { error: `Camoufox: ${e.message}` };
  }
}

// ========================
// 下载视频
// ========================
async function downloadVideo(videoUrl, outPath) {
  const qUrl = videoUrl.replace(/"/g, '\\"');
  const qPath = outPath.replace(/"/g, '\\"');
  execSync(`curl -sL -o "${qPath}" "${qUrl}" -H "Referer: https://www.douyin.com/"`, {
    timeout: 120000, stdio: 'pipe'
  });
  const size = fs.statSync(outPath).size;
  if (size < 10000) throw new Error(`File too small (${(size/1024).toFixed(1)}KB)`);
  return (size / 1024 / 1024).toFixed(2);
}

// ========================
// 抽帧
// ========================
async function extractFrames(videoPath, framesDir) {
  fs.mkdirSync(framesDir, { recursive: true });
  const qV = videoPath.replace(/"/g, '\\"');
  const qF = framesDir.replace(/"/g, '\\"');
  const framePattern = `${qF}/frame_%03d.jpg`;
  try {
    execSync(`ffmpeg -i "${qV}" -vf "fps=1" -q:v 2 "${framePattern}" 2>&1`, {
      timeout: 60000, stdio: 'pipe'
    });
  } catch (e) {
    const tempPath = path.join(path.dirname(videoPath), 'temp_' + path.basename(videoPath));
    execSync(`ffmpeg -i "${qV}" -c:v libx264 -crf 23 "${tempPath}" 2>&1`, {
      timeout: 120000, stdio: 'pipe'
    });
    execSync(`ffmpeg -i "${tempPath}" -vf "fps=1" -q:v 2 "${framePattern}" 2>&1`, {
      timeout: 60000, stdio: 'pipe'
    });
    if (fs.existsSync(tempPath)) fs.unlinkSync(tempPath);
  }
  return fs.readdirSync(framesDir).filter(f => f.endsWith('.jpg')).sort();
}

// ========================
// 文案提取 + 交叉验证 + 标点分段
// ========================
async function extractAndValidateTranscript(videoPath, videoId, videoInfo, framesDir) {
  const whisperFile = path.join(TMP_DIR, `whisper_${videoId}.txt`);

  // Step 1: Whisper 语音识别（使用常驻服务）
  console.log(`    🎙️ Whisper 语音识别（常驻服务）...`);
  try {
    // 统一使用 medium（在 Apple Silicon 上比 large-v3 快 2-3 倍）
    const model = 'medium';

    const result = await whisperTranscribe(videoPath, videoId, model);

    if (!result.ok) {
      throw new Error(result.error);
    }

    const whisperText = result.text.trim();
    if (!whisperText) return null;

    fs.writeFileSync(whisperFile, whisperText, 'utf-8');
    console.log(`    ✓ Whisper: ${whisperText.length}字`);

    // Step 2: 交叉验证 + 标点分段
    const subtitleUrl = videoInfo.subtitleUrl || 'none';
    console.log(`    🔍 交叉验证 + 自动标点分段...`);

    const corrected = execSync(`"${SYSTEM_PYTHON}" "${CROSS_VALIDATE_PY}" "${whisperFile}" "${subtitleUrl}" "${framesDir}" "${videoId}" "${videoInfo.desc || ''}" "${apiKey}"`, {
      timeout: 300000, stdio: ['pipe', 'pipe', 'inherit'], env: getEnv(),
    }).toString().trim();

    // 最后一行是修正后的文案（cross_validate.py 最后一行打印了纯文案）
    const correctedText = corrected.split('\n').filter(l => l.startsWith('[验证]')).length > 0
      ? corrected.split('[验证] 修正完成:')[1]?.split('\n').slice(1).join('\n').trim()
      : corrected.trim();

    if (!correctedText) return whisperText; // fallback
    console.log(`    ✓ 修正+标点: ${correctedText.length}字`);

    return correctedText;
  } catch (e) {
    console.log(`    ⚠️ 文案提取失败: ${e.message}`);
    // 清理
    if (fs.existsSync(whisperFile)) fs.unlinkSync(whisperFile);
    return null;
  }
}

// ========================
// 多维度蒸馏分析
// ========================
async function analyzeDimension(framesDir, frames, videoInfo, dimension, transcript) {
  const selected = frames.filter((_, i) => i % 10 === 0).slice(0, 20);
  if (!selected.length && !transcript) return null;

  const promptPath = path.join(PROMPTS_DIR, dimension.file);
  const prompt = fs.readFileSync(promptPath, 'utf-8');

  const content = [
    { type: 'text', text: prompt }
  ];

  content.push({
    type: 'text',
    text: `\n---\n视频信息：\n- 标题：${videoInfo.desc || '未知'}\n- 作者：${videoInfo.author || '未知'}\n- 时长：${videoInfo.duration || '未知'}秒\n- 标签：${(videoInfo.hashtags || []).join(', ')}\n`
  });

  if (transcript) {
    content.push({
      type: 'text',
      text: `\n---\n完整文案（已交叉验证+标点分段）：\n${transcript}\n`
    });
  }

  for (const f of selected) {
    const b64 = fs.readFileSync(path.join(framesDir, f), 'base64');
    content.push({ type: 'image_url', image_url: { url: `data:image/jpeg;base64,${b64}` } });
  }

  const resp = await client.chat.completions.create({
    model: ANALYSIS_MODEL,
    messages: [{ role: 'user', content }],
    max_tokens: 8192
  });
  return resp.choices[0].message.content;
}

// ========================
// 合并精炼输出（v4 核心改动）
// ========================
async function generateMergedOutput(allResults, dimension, existingContent) {
  const promptPath = path.join(PROMPTS_DIR, dimension.file);
  const dimensionPrompt = fs.readFileSync(promptPath, 'utf-8');

  // 构建输入
  let inputText = dimensionPrompt + '\n\n## 输入数据\n\n';
  for (let i = 0; i < allResults.length; i++) {
    const r = allResults[i];
    if (r[dimension.name]) {
      inputText += `\n### 视频 ${i + 1}: ${r.info.desc || r.videoId} (${r.info.duration}s)\n`;
      inputText += `文案: ${(r.transcript || '').substring(0, 500)}...\n\n`;
      inputText += `## ${dimension.name} 分析\n${r[dimension.name]}\n\n`;
    }
  }

  // 如果有已有内容，附加上
  let contextNote = '';
  if (existingContent) {
    contextNote = `\n## 已有最终文件内容（需要增量更新，不要重复已有内容）\n${existingContent.substring(0, 5000)}...\n`;
  }

  const mergePrompt = `你是资深内容策略师，负责将多个视频的${dimension.name}分析合并为一份精炼的最终输出文件。

## 任务
将输入的所有视频分析结果，合并为一份精炼的${dimension.output}文件。

## 输出要求
1. **不是逐个拼接**！要合并提炼共性规律、公式、模板
2. **去重**：多个视频发现的相同规律只写一次
3. **结构化**：清晰的分类和层级
4. **可操作**：生成的内容能直接照着执行
5. **精炼**：每个点只写关键规律+1-2个示例，不写完整分析
${contextNote ? '6. **增量更新**：已有文件中已有的内容不要重复写入，只补充新发现\n' : ''}

## 格式要求
- 元数据头部标注：基于视频数、最后更新时间、版本
- 核心规律用公式/模板格式呈现
- 每个规律标注出现频率（如"5/5 视频出现"）

${contextNote ? '注意：已有内容已在下方标注，你只需要合并新发现，不要重复已有内容。' : ''}

请直接输出最终文件内容，不要任何解释。`;

  const content = [
    { type: 'text', text: mergePrompt },
    { type: 'text', text: inputText }
  ];

  // 尝试主模型，失败则切备用
  let resp;
  try {
    resp = await client.chat.completions.create({
      model: FINAL_MODEL,
      messages: [{ role: 'user', content }],
      max_tokens: 16384,
      thinking: { type: 'enabled', budget_tokens: 4096 }
    });
  } catch (e) {
    console.log(`    ⚠️ ${FINAL_MODEL} 失败 (${e.message}), 切换备用 ${FINAL_MODEL_FALLBACK}...`);
    resp = await client.chat.completions.create({
      model: FINAL_MODEL_FALLBACK,
      messages: [{ role: 'user', content }],
      max_tokens: 16384,
      thinking: { type: 'enabled', budget_tokens: 4096 }
    });
  }
  return resp.choices[0].message.content;
}

// ========================
// 主流程
// ========================
async function main() {
  console.log('=== 视频创作者方法论蒸馏 v5（常驻Whisper服务 + 蒸馏延迟 + 保留视频）===\n');
  console.log(`视频数量: ${videoIds.length}`);
  console.log(`输出目录: ${outputDir}`);
  console.log(`蒸馏模型: ${ANALYSIS_MODEL}（多模态）`);
  console.log(`合并模型: ${FINAL_MODEL} + 思考模式（备用: ${FINAL_MODEL_FALLBACK}）\n`);

  // 检查环境
  console.log('检查环境...');
  try {
    execSync(`"${SYSTEM_PYTHON}" -c "from faster_whisper import WhisperModel; print('OK')"`, {
      stdio: ['pipe', 'pipe', 'pipe']
    }).toString().trim();
    console.log('  ✅ Whisper 已就绪（系统python）');
  } catch (e) {
    console.log('  ❌ Whisper 未安装');
    return;
  }
  try {
    execSync(`"${CAMOUFOX_PYTHON}" -c "import camoufox"`, {
      stdio: ['pipe', 'pipe', 'pipe']
    }).toString().trim();
    console.log('  ✅ Camoufox 已就绪（虚拟环境）\n');
  } catch (e) {
    console.log('  ❌ Camoufox 未安装');
    return;
  }

  // 启动常驻 Whisper 服务（一次加载，全程复用）
  await startWhisperService();

  const allResults = [];

  // 阶段一：逐视频分析
  console.log('=== 阶段一：单视频多维度蒸馏 ===\n');

  for (let i = 0; i < videoIds.length; i++) {
    const videoId = videoIds[i];
    console.log(`\n[${i + 1}/${videoIds.length}] 视频 ${videoId}...`);

    const videoPath = path.join(TMP_DIR, `${videoId}.mp4`);
    const framesDir = path.join(TMP_DIR, `${videoId}_frames`);
    const result = { videoId, info: {}, transcript: null };
    for (const dim of DIMENSIONS) result[dim.name] = null;

    try {
      // 获取视频信息
      const info = await getVideoInfoCamoufox(videoId);
      if (info.error || !info.playUrl) throw new Error(info.error || 'No CDN URL');
      result.info = info;
      const subNote = info.subtitleUrl ? '📝有字幕' : '📝无字幕';
      console.log(`  ✓ 信息: ${info.desc?.substring(0, 50)} (${info.duration}s) ${subNote}`);

      // 下载视频
      const size = await downloadVideo(info.playUrl, videoPath);
      console.log(`  ✓ 下载: ${size}MB`);

      // 抽帧
      const frames = await extractFrames(videoPath, framesDir);
      console.log(`  ✓ 帧: ${frames.length}张`);

      // 文案提取 + 交叉验证 + 标点分段
      let transcript = null;
      try {
        transcript = await extractAndValidateTranscript(videoPath, videoId, info, framesDir);
        if (transcript) {
          const transcriptFile = path.join(TRANSCRIPT_DIR, `video_${videoId}.md`);
          const mdContent = `# 视频文案 - ${videoId}\n\n> 标题：${info.desc || '未知'}\n> 作者：${info.author || '未知'}\n> 时长：${info.duration || '未知'}秒\n> 提取时间：${new Date().toLocaleString('zh-CN')}\n> 提取方式：Whisper + 交叉验证 + LLM标点分段\n\n---\n\n## 完整文案\n\n${transcript}\n`;
          fs.writeFileSync(transcriptFile, mdContent, 'utf-8');
          console.log(`  ✓ 文案: ${transcript.length}字 → 永久保存`);
          result.transcript = transcript;
        }
      } catch (e) {
        console.log(`  ⚠️ 文案失败: ${e.message}`);
      }

      // 资料完整性检查：缺少任何核心资料，跳过蒸馏（宁缺毋滥）
      const missingMaterials = [];
      if (!frames || frames.length === 0) missingMaterials.push('帧图');
      if (!transcript || transcript.length === 0) missingMaterials.push('文案');

      if (missingMaterials.length > 0) {
        console.log(`  🚫 资料不全，跳过蒸馏：缺少 ${missingMaterials.join('、')}`);
        console.log(`     （宁缺毋滥：资料不全的分析结果是垃圾，浪费 API 调用）`);
        result.distillSkipped = true;
        result.skipReason = `缺少 ${missingMaterials.join('、')}`;
      } else {
        // 多维度蒸馏（每个维度之间加随机延迟，防止触发频率限制）
        for (const dim of DIMENSIONS) {
          console.log(`  ⏳ 蒸馏: ${dim.name}...`);
          const analysis = await analyzeDimension(framesDir, frames, info, dim, transcript);
          result[dim.name] = analysis;

          const singleFile = path.join(SINGLE_DIR, `${videoId}_${dim.name}.md`);
          fs.writeFileSync(singleFile, analysis || '❌ 分析返回为空', 'utf-8');
          console.log(`  ✓ ${dim.name}`);

          // 维度之间加延迟（最后一个维度不需要延迟）
          if (dim.name !== DIMENSIONS[DIMENSIONS.length - 1].name) {
            await randomDelay(DISTILL_DELAY.minMs, DISTILL_DELAY.maxMs);
          }
        }
      }

      allResults.push(result);

    } catch (e) {
      console.log(`  ❌ ${e.message}`);
      result.error = e.message;
      allResults.push(result);
    }

    // 保留视频到视频保留目录（不删除，等老板确认）
    const keepVideoPath = path.join(VIDEO_KEEP_DIR, `${videoId}.mp4`);
    if (!fs.existsSync(keepVideoPath) && fs.existsSync(videoPath)) {
      fs.copyFileSync(videoPath, keepVideoPath);
    }

    // 清理临时帧
    if (fs.existsSync(framesDir)) fs.rmSync(framesDir, { recursive: true, force: true });

    // 注意：v5 不再删除原始视频文件！等老板确认无误后再手动清理
    // TMP_DIR 中的视频文件会一直保留

    // 保存进度
    saveProgress(allResults, outputDir);

    // 视频间延迟（防封）
    if (i < videoIds.length - 1) {
      await randomDelay(ANTI_BAN.minDelay, ANTI_BAN.maxDelay);
    }
  }

  // 停止 Whisper 常驻服务
  await stopWhisperService();

  // 阶段二：合并精炼输出（只使用资料完整的视频）
  const validResults = allResults.filter(r => !r.error && !r.distillSkipped);
  const skippedResults = allResults.filter(r => r.distillSkipped);
  if (skippedResults.length > 0) {
    console.log(`\n  🚫 ${skippedResults.length} 个视频因资料不全跳过蒸馏：`);
    for (const r of skippedResults) {
      console.log(`     - ${r.videoId} (${r.info.desc?.substring(0, 30) || ''}): ${r.skipReason}`);
    }
  }
  if (validResults.length >= 1) {
    console.log('\n\n=== 阶段二：合并精炼输出（最强模型 + 思考模式）===\n');

    for (const dim of DIMENSIONS) {
      console.log(`  ⏳ 合并: ${dim.output}...`);

      // 读取已有内容（增量更新支持）
      const finalPath = path.join(outputDir, dim.output);
      let existingContent = null;
      if (fs.existsSync(finalPath)) {
        existingContent = fs.readFileSync(finalPath, 'utf-8');
        console.log(`    📂 已有文件，进行增量更新...`);
      }

      const merged = await generateMergedOutput(validResults, dim, existingContent);
      fs.writeFileSync(finalPath, merged, 'utf-8');
      console.log(`  ✓ ${dim.output}（${merged.length}字）`);
    }
  }

  // 统计
  const done = allResults.filter(r => !r.error && !r.distillSkipped).length;
  const skipped = allResults.filter(r => r.distillSkipped).length;
  const failed = allResults.filter(r => r.error).length;
  console.log(`\n✅ 全部完成！`);
  console.log(`   📊 资料齐全+蒸馏完成: ${done}`);
  console.log(`   🚫 资料不全跳过蒸馏: ${skipped}`);
  console.log(`   ❌ 下载/处理失败: ${failed}`);
  console.log(`📁 输出目录: ${outputDir}`);
  console.log(`💾 原始视频保留在: ${VIDEO_KEEP_DIR}（等老板确认后再清理）`);
  console.log(`💾 临时视频保留在: ${TMP_DIR}（等老板确认后再清理）`);
  console.log(`💡 修好缺失的资料后，可以重新跑跳过蒸馏的视频`);
}

function saveProgress(results, outputDir) {
  const summary = results.map((r) => ({
    videoId: r.videoId,
    desc: r.info.desc || '',
    status: r.error ? 'failed' : (r.distillSkipped ? 'skipped' : 'done'),
    error: r.error || null,
    skipReason: r.skipReason || null
  }));
  fs.writeFileSync(path.join(outputDir, '进度.json'), JSON.stringify(summary, null, 2), 'utf-8');
}

main().catch(e => {
  console.error('Fatal:', e.message);
  console.error(e.stack);
  process.exit(1);
});
