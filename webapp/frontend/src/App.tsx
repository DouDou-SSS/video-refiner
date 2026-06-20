import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  BarChart3,
  Boxes,
  ClipboardList,
  Cpu,
  Database,
  Eye,
  FileText,
  FolderOpen,
  Gauge,
  Home,
  Layers3,
  Play,
  RefreshCcw,
  Save,
  Settings,
  ShieldCheck,
  Square,
  TestTube2,
  Trash2,
  Video,
} from 'lucide-react';

const API = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:7860';

type Preset = {
  key: string;
  provider_name: string;
  base_url: string;
  analysis_model: string;
  merge_model: string;
  supports_vision: boolean;
  supports_reasoning: boolean;
  max_tokens: number;
  temperature: number;
};

type ModelProfile = Preset & {
  id: string;
  provider_key: string;
  key_storage?: string;
  is_tested: boolean;
  test_result?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

type Job = {
  id: string;
  status: string;
  input_type: string;
  output_dir: string;
  model_profile_id: string;
  max_videos: number;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  error?: string;
  videos?: Array<Record<string, string>>;
  dimensions?: Array<Record<string, string>>;
  artifacts?: Array<Record<string, string>>;
  cleanup_sizes?: Record<string, { bytes: number; count: number }>;
};

type Preflight = {
  ok: boolean;
  checks: Array<{ name: string; ok: boolean; detail: string; required: boolean }>;
};

type LogLine = { id: number; ts: string; level: string; message: string };
type Tab = 'home' | 'models' | 'jobs' | 'run';
type CleanupCategory = 'frames' | 'single_analysis' | 'kept_videos' | 'transcripts' | 'raw_data';
type VideoAutomationExportResult = {
  ok: boolean;
  path: string;
  manifest: {
    productName: string;
    creator: string;
    platform: string;
    videoCount: number;
    generatedAt: string;
    schemaVersion: string;
  };
  validation: {
    status: 'passed';
    validVideoCount: number;
    failedVideoCount: number;
  };
  file_count: number;
};

type EvidenceObservation = {
  visual_description?: string;
  shot_type?: string;
  composition?: string;
  on_screen_text_observation?: string;
  transition_observation?: string;
  confidence?: string;
  uncertainty?: string;
};

type EvidenceShot = {
  evidence_id: string;
  start_seconds: number;
  end_seconds: number;
  time_range: string;
  keyframe: string;
  keyframe_url?: string;
  scene_score?: number;
  segment_type?: 'evidence_window' | 'detected_cut_segment';
  boundary_source?: 'uniform_coverage' | 'scene_peak' | 'detected_cut';
  boundary_confidence?: 'high' | 'medium' | 'low';
  transcript_excerpt?: string;
  ocr_excerpt?: string;
  text_alignment?: string;
  visual_observation?: EvidenceObservation;
};

type EvidenceQuality = {
  transcript_alignment?: 'timed' | 'coarse';
  alignment_status?: 'timed' | 'coarse';
  observation_coverage?: 'complete' | 'partial';
  visual_confidence_summary?: { high?: number; medium?: number; low?: number };
  eligible_for_precise_timing?: boolean;
};

type EvidenceTimeline = {
  video_id: string;
  duration_seconds: number;
  quality?: EvidenceQuality;
  scene_curve?: Array<{ timestamp_seconds: number; score: number }>;
  shots: EvidenceShot[];
};

const CLEANUP_OPTIONS: Array<{ key: CleanupCategory; label: string; description: string }> = [
  { key: 'frames', label: '帧图片', description: '原始数据里的 *_frames 目录' },
  { key: 'single_analysis', label: '单视频分析', description: '单视频分析目录' },
  { key: 'kept_videos', label: '视频保留', description: '视频保留目录' },
  { key: 'transcripts', label: '文案', description: '文案目录' },
  { key: 'raw_data', label: '原始数据', description: '下载视频和转写中间文件' },
];

function evidenceSegmentLabel(type?: string) {
  return type === 'detected_cut_segment' ? '检测切段' : '证据窗口';
}

function evidenceBoundaryLabel(source?: string) {
  if (source === 'detected_cut') return '场景检测切点';
  if (source === 'scene_peak') return '场景峰值采样';
  return '均匀采样';
}

const PIPELINE_STEPS = [
  ['01', '环境预检', '确认 ffmpeg、Whisper、OCR、下载组件和模型配置可用。'],
  ['02', '解析与下载', '解析单视频、批量链接或博主主页，按阶梯方案下载视频。'],
  ['03', '抽帧与文案', '按默认策略或自定义间隔抽帧；软字幕优先，底部硬字幕足够时 OCR 可作为主文案，Whisper 失败会降级重试。'],
  ['04', '证据时间线', '场景变化、关键帧、图文时间关联和视觉证据只生成一次，供后续步骤复用。'],
  ['05', '资料检查', '跳过资料不完整的视频，失败进入明确状态并支持自动重试。'],
  ['06', '5 个单视频维度', '每项关键判断都引用证据时间线，随后产出 5 份兼容旧结构的文档。'],
  ['07', 'Benchmark Intelligence', '生成 creator profile、pattern library、QA checklist、video cards 和 retrieval pack。'],
];

const VISIBLE_ARTIFACT_KINDS = [
  'final_output',
  'benchmark_profile',
  'benchmark_pattern_library',
  'benchmark_qa_checklist',
  'retrieval_index',
  'retrieval_pack',
  'raw_refs',
  'visual_timeline',
  'manifest',
  'progress',
];

const ARTIFACT_LABELS: Record<string, string> = {
  final_output: '旧版 5 个单视频维度文档',
  benchmark_profile: 'Creator Profile',
  benchmark_pattern_library: 'Pattern Library',
  benchmark_qa_checklist: 'QA Checklist',
  retrieval_index: 'Retrieval Index',
  retrieval_pack: 'Retrieval Pack',
  raw_refs: 'Raw 引用',
  visual_timeline: '视觉证据时间线',
  manifest: 'Manifest',
  progress: '进度',
};

const CUSTOM_FRAME_INTERVAL_DEFAULT_SECONDS = 5;
const FRAME_ESTIMATE_DURATIONS = [5, 10, 20, 30, 50];
const ESTIMATED_FRAME_BYTES = 220 * 1024;
const MODEL_DIMENSION_COUNT = 5;
const MODEL_MAX_FRAMES_PER_DIMENSION = 20;

const emptyCleanupSelection = (): Record<CleanupCategory, boolean> => ({
  frames: false,
  single_analysis: false,
  kept_videos: false,
  transcripts: false,
  raw_data: false,
});

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      detail = parsed.detail || text;
    } catch {
      detail = text;
    }
    throw new Error(detail || `${res.status}`);
  }
  return res.json();
}

const emptyProfile = (preset?: Preset) => ({
  id: undefined as string | undefined,
  provider_key: preset?.key || 'custom',
  provider_name: preset?.provider_name || '自定义 OpenAI-compatible',
  base_url: preset?.base_url || '',
  api_key: '',
  analysis_model: preset?.analysis_model || '',
  merge_model: preset?.merge_model || '',
  supports_vision: preset?.supports_vision ?? true,
  supports_reasoning: preset?.supports_reasoning ?? false,
  max_tokens: preset?.max_tokens || 8192,
  temperature: preset?.temperature ?? 0.2,
});

function computeJobProgress(job: Job, logs: LogLine[] = []) {
  const videos = job.videos || [];
  const dimensions = job.dimensions || [];
  const isActive = ['queued', 'running'].includes(job.status);
  const terminalStatuses = isActive ? ['done', 'skipped'] : ['done', 'skipped', 'failed'];
  const totalVideos = videos.length;
  const totalUnits = Math.max(totalVideos, 1);
  const statusWeight: Record<string, number> = {
    pending: 0,
    downloading: 0.12,
    framing: 0.24,
    transcribing: 0.38,
    evidencing: 0.47,
    distilling: 0.48,
    retry_wait: 0.05,
    done: 1,
    skipped: 1,
    failed: isActive ? 0 : 1,
  };

  const currentVideo = isActive ? videos.find((video) => !terminalStatuses.includes(String(video.status))) : undefined;
  const currentDimension = currentVideo
    ? dimensions.find((dimension) => dimension.video_db_id === currentVideo.id && dimension.status === 'running')
    : undefined;
  const currentStep =
    currentVideo?.status === 'retry_wait'
      ? '等待自动重试'
      : currentVideo?.status === 'failed'
        ? '等待自动重试'
        : currentVideo?.status;

  const progressSum = videos.reduce((sum, video) => {
    if (video.status === 'distilling') {
      const related = dimensions.filter((dimension) => dimension.video_db_id === video.id);
      const doneDims = related.filter((dimension) => dimension.status === 'done').length;
      return sum + 0.48 + (Math.min(doneDims, 5) / 5) * 0.42;
    }
    return sum + (statusWeight[String(video.status)] ?? 0);
  }, 0);

  const terminalCount = videos.filter((video) => terminalStatuses.includes(String(video.status))).length;
  const doneCount = videos.filter((video) => video.status === 'done').length;
  const failedCount = videos.filter((video) => video.status === 'failed').length;
  const retryWaitCount = videos.filter((video) => video.status === 'retry_wait').length;
  const skippedCount = videos.filter((video) => video.status === 'skipped').length;
  const videoPercent = totalVideos ? Math.max(0, Math.min(100, Math.round((progressSum / totalUnits) * 100))) : 0;
  let percent = job.status === 'running' ? Math.round(videoPercent * 0.8) : videoPercent;
  let currentLabel = currentVideo
    ? `${currentVideo.video_id || '当前视频'} · ${currentDimension?.dimension || currentStep}`
    : job.status === 'running'
      ? '跨视频合并精炼中'
      : job.status === 'partial_done'
        ? '已生成阶段性结果，可重试失败视频'
      : job.status === 'failed'
        ? '任务已停止，可点击重试继续'
      : '无运行中步骤';

  if (job.status === 'running' && !currentVideo && totalVideos > 0) {
    if (job.error) {
      percent = Math.min(99, Math.max(percent, 98));
      currentLabel = 'Benchmark Intelligence 等待云端模型自动重试';
    }
    const mergeDimensions = new Set(
      logs
        .map((line) => line.message.match(/^合并输出：(.+)$/)?.[1])
        .filter((value): value is string => Boolean(value)),
    );
    const benchmarkLine = [...logs]
      .reverse()
      .map((line) => ({ line, match: line.message.match(/Benchmark 视频卡片批次 (\d+)\/(\d+)/) }))
      .find((item) => item.match);

    if (job.error) {
      // 任务级错误已经在详情区单独展示，这里只保留阶段名，避免进度条文案过长。
    } else if (benchmarkLine?.match) {
      const batch = Number(benchmarkLine.match[1]);
      const totalBatches = Number(benchmarkLine.match[2]);
      const completedBatches = Math.max(0, Math.min(totalBatches, batch - 1));
      percent = Math.min(98, 90 + Math.round((completedBatches / Math.max(1, totalBatches)) * 8));
      currentLabel = batch >= totalBatches
        ? `Benchmark Intelligence：第 ${batch}/${totalBatches} 批处理中，随后生成账号级汇总`
        : `Benchmark Intelligence：第 ${batch}/${totalBatches} 批处理中`;
    } else {
      percent = Math.min(90, 80 + mergeDimensions.size * 2);
      currentLabel = mergeDimensions.size
        ? `跨视频合并精炼：${mergeDimensions.size}/5 个维度完成`
        : '跨视频合并精炼中';
    }
  }

  if (job.status === 'done') {
    percent = 100;
    currentLabel = '全部流程已完成，可以导出';
  }

  return {
    percent,
    totalVideos,
    terminalCount,
    doneCount,
    failedCount,
    retryWaitCount,
    skippedCount,
    currentLabel,
  };
}

function visibleArtifacts(job: Job) {
  const seen = new Set<string>();
  return (job.artifacts || [])
    .filter((item) => VISIBLE_ARTIFACT_KINDS.includes(item.kind))
    .filter((item) => {
      const key = `${item.kind}:${item.path}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function latestJobsByOutputDir(jobs: Job[]) {
  const seen = new Set<string>();
  const result: Job[] = [];
  for (const job of jobs) {
    const key = displayJobName(job.output_dir || job.id);
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(job);
  }
  return result;
}

function artifactDisplayName(item: Record<string, string>) {
  const label = ARTIFACT_LABELS[item.kind] || item.kind;
  const parts = String(item.path || '').split(/[\\/]+/).filter(Boolean);
  const name = parts[parts.length - 1] || item.path;
  return `${label} · ${name}`;
}

function videoStatusMessage(video: Record<string, string>) {
  const retryCount = Number(video.retry_count || 0);
  const message = video.error || video.skip_reason || '';
  const nextRetry = video.next_retry_at ? `下次自动重试：${formatTime(video.next_retry_at)}` : '';
  if (!retryCount && !nextRetry) return message;
  return [message, retryCount ? `已自动尝试 ${retryCount} 次` : '', nextRetry].filter(Boolean).join(' · ');
}

function displayVideoStatus(status: string) {
  if (status === 'retry_wait') return '等待自动重试';
  return status;
}

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function formatDurationEstimate(seconds: number) {
  if (seconds < 60) return `约 ${Math.max(1, Math.round(seconds))} 秒`;
  return `约 ${(seconds / 60).toFixed(seconds < 600 ? 1 : 0)} 分钟`;
}

function defaultFrameInterval(durationMinutes: number) {
  return durationMinutes <= 10 ? 1 : 5;
}

function frameEstimate(durationMinutes: number, customIntervalEnabled: boolean, customIntervalSeconds: number) {
  const intervalSeconds = customIntervalEnabled ? customIntervalSeconds : defaultFrameInterval(durationMinutes);
  const durationSeconds = durationMinutes * 60;
  const frameCount = Math.ceil(durationSeconds / Math.max(1, intervalSeconds));
  const storageBytes = frameCount * ESTIMATED_FRAME_BYTES;
  const modelFrameCount = Math.min(frameCount, MODEL_MAX_FRAMES_PER_DIMENSION) * MODEL_DIMENSION_COUNT;
  const modelTimeSeconds = MODEL_DIMENSION_COUNT * 55 + modelFrameCount * 1.2;
  return { intervalSeconds, frameCount, storageBytes, modelFrameCount, modelTimeSeconds };
}

function displayJobName(outputDir: string) {
  const parts = outputDir.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] || outputDir || '未命名任务';
}

function displayVideoTitle(video: Record<string, string>) {
  return String(video.title || video.desc || '').trim() || '未获取标题';
}

function computeVideoProgress(video: Record<string, string>, dimensions: Array<Record<string, string>>, jobIsActive: boolean) {
  const status = String(video.status || '');
  if (status === 'distilling') {
    const related = dimensions.filter((dimension) => dimension.video_db_id === video.id);
    const doneDims = related.filter((dimension) => dimension.status === 'done').length;
    return Math.round(48 + (Math.min(doneDims, 5) / 5) * 42);
  }

  const statusWeight: Record<string, number> = {
    pending: 0,
    retry_wait: 0,
    downloading: 12,
    framing: 24,
    transcribing: 38,
    evidencing: 47,
    done: 100,
    skipped: 100,
    failed: jobIsActive ? 0 : 100,
  };
  return statusWeight[status] ?? 0;
}

function statusClass(status: string) {
  return status.replace(/[^a-z0-9_-]/gi, '-').toLowerCase() || 'unknown';
}

export function App() {
  const [tab, setTab] = useState<Tab>('home');
  const [presets, setPresets] = useState<Preset[]>([]);
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [selectedPreset, setSelectedPreset] = useState('bailian');
  const [profileForm, setProfileForm] = useState(emptyProfile());
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [activeJob, setActiveJob] = useState<Job | null>(null);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [testingProfileId, setTestingProfileId] = useState<string | null>(null);
  const [deletingProfileId, setDeletingProfileId] = useState<string | null>(null);
  const [cleanupSelection, setCleanupSelection] = useState<Record<CleanupCategory, boolean>>(emptyCleanupSelection);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [metadataRefreshBusy, setMetadataRefreshBusy] = useState(false);
  const [videoAutomationExportPath, setVideoAutomationExportPath] = useState('');
  const [evidenceTimeline, setEvidenceTimeline] = useState<EvidenceTimeline | null>(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const streamRef = useRef<EventSource | null>(null);
  const selectedJobIdRef = useRef<string | null>(null);
  const detailRequestSeqRef = useRef(0);
  const [jobForm, setJobForm] = useState({
    input_type: 'batch',
    inputs: '',
    output_dir: '',
    model_profile_id: '',
    max_videos: 50,
    frame_interval_seconds: CUSTOM_FRAME_INTERVAL_DEFAULT_SECONDS,
    frame_interval_custom: false,
  });
  const [message, setMessage] = useState('');

  const testedProfiles = useMemo(() => profiles.filter((item) => item.is_tested && item.supports_vision), [profiles]);
  const selectedCleanupCategories = CLEANUP_OPTIONS.filter((item) => cleanupSelection[item.key]).map((item) => item.key);
  const runningJobs = jobs.filter((job) => ['queued', 'running'].includes(job.status)).length;
  const latestJob = jobs[0];
  const activeJobCanExport = Boolean(activeJob && ['done', 'partial_done'].includes(activeJob.status));
  const taskListJobs = useMemo(() => latestJobsByOutputDir(jobs), [jobs]);

  async function loadAll() {
    const [presetData, profileData, jobData] = await Promise.all([
      api<Preset[]>('/api/provider-presets'),
      api<ModelProfile[]>('/api/model-profiles'),
      api<Job[]>('/api/jobs'),
    ]);
    setPresets(presetData);
    setProfiles(profileData);
    setJobs(jobData);
    if (!jobForm.model_profile_id && profileData.length) {
      setJobForm((prev) => ({ ...prev, model_profile_id: profileData[0].id }));
    }
    if (!profileForm.base_url && presetData.length) {
      setProfileForm(emptyProfile(presetData[0]));
    }
  }

  useEffect(() => {
    loadAll().catch((error) => setMessage(error.message));
  }, []);

  useEffect(() => {
    return () => {
      streamRef.current?.close();
    };
  }, []);

  useEffect(() => {
    const preset = presets.find((item) => item.key === selectedPreset);
    if (preset) setProfileForm(emptyProfile(preset));
  }, [selectedPreset]);

  useEffect(() => {
    if (!activeJob || !['queued', 'running'].includes(activeJob.status)) return;
    const timer = window.setInterval(() => {
      refreshJob(activeJob.id).catch(console.error);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [activeJob?.id, activeJob?.status]);

  async function runPreflight() {
    if (preflightBusy) return;
    setPreflightBusy(true);
    setMessage('正在预检本机环境，请稍等...');
    try {
      const result = await api<Preflight>('/api/preflight', { method: 'POST' });
      setPreflight(result);
      setMessage(result.ok ? '预检通过。' : '预检完成，但有必需组件未通过，请查看下方结果。');
    } catch (error) {
      setMessage(`预检失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setPreflightBusy(false);
    }
  }

  async function saveProfile() {
    try {
      const saved = await api<ModelProfile>('/api/model-profiles', {
        method: 'POST',
        body: JSON.stringify(profileForm),
      });
      setMessage(`模型配置已保存：${saved.provider_name}`);
      await loadAll();
    } catch (error) {
      setMessage(`保存失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  async function testProfile(id: string) {
    setTestingProfileId(id);
    setMessage('正在测试模型连接...');
    try {
      const result = await api<Record<string, unknown>>(`/api/model-profiles/${id}/test`, { method: 'POST' });
      const errors = Array.isArray(result.errors) && result.errors.length ? `：${result.errors.join('；')}` : '';
      setMessage(`${String(result.message || '测试完成')}${errors}`);
      await loadAll();
    } catch (error) {
      setMessage(`测试失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setTestingProfileId(null);
    }
  }

  async function deleteProfile(id: string, name: string) {
    if (!window.confirm(`删除模型配置「${name}」？对应 API Key 也会一起删除。`)) return;
    setDeletingProfileId(id);
    setMessage('正在删除模型配置...');
    try {
      await api(`/api/model-profiles/${id}`, { method: 'DELETE' });
      setMessage(`已删除模型配置：${name}`);
      if (jobForm.model_profile_id === id) {
        setJobForm((prev) => ({ ...prev, model_profile_id: '' }));
      }
      await loadAll();
    } catch (error) {
      setMessage(`删除失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setDeletingProfileId(null);
    }
  }

  async function createJob() {
    const inputs = jobForm.inputs
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    const job = await api<Job>('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({
        input_type: jobForm.input_type,
        inputs,
        output_dir: jobForm.output_dir || null,
        model_profile_id: jobForm.model_profile_id,
        max_videos: Number(jobForm.max_videos),
        frame_interval_seconds: jobForm.frame_interval_custom ? Number(jobForm.frame_interval_seconds) : null,
      }),
    });
    setActiveJob(job);
    setTab('jobs');
    watchJob(job.id);
    await loadAll();
  }

  async function refreshJob(id: string, seq = detailRequestSeqRef.current) {
    const detail = await api<Job>(`/api/jobs/${id}`);
    if (selectedJobIdRef.current !== id || detailRequestSeqRef.current !== seq) return;
    setActiveJob(detail);
    await loadAll();
  }

  function selectJob(id: string) {
    detailRequestSeqRef.current += 1;
    selectedJobIdRef.current = id;
    streamRef.current?.close();
    streamRef.current = null;
    setLogs([]);
    setEvidenceTimeline(null);
    refreshJob(id).catch(console.error);
  }

  function watchJob(id: string) {
    detailRequestSeqRef.current += 1;
    const seq = detailRequestSeqRef.current;
    selectedJobIdRef.current = id;
    streamRef.current?.close();
    setLogs([]);
    const stream = new EventSource(`${API}/api/jobs/${id}/events`);
    streamRef.current = stream;
    stream.onmessage = (event) => {
      if (selectedJobIdRef.current !== id || detailRequestSeqRef.current !== seq) {
        stream.close();
        return;
      }
      const line = JSON.parse(event.data);
      if (line.message === '[stream-end]') {
        stream.close();
        if (streamRef.current === stream) streamRef.current = null;
        refreshJob(id, seq).catch(console.error);
        return;
      }
      setLogs((prev) => [...prev.slice(-250), line]);
      refreshJob(id, seq).catch(console.error);
    };
    stream.onerror = () => {
      stream.close();
      if (streamRef.current === stream) streamRef.current = null;
    };
  }

  async function cancelJob(id: string) {
    try {
      await api(`/api/jobs/${id}/cancel`, { method: 'POST' });
      setMessage('已发送取消请求');
      await refreshJob(id);
    } catch (error) {
      setMessage(`取消失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  async function retryJob(id: string) {
    try {
      await api(`/api/jobs/${id}/retry`, { method: 'POST' });
      setMessage('已开始重试，后续失败会自动重试');
      watchJob(id);
      await refreshJob(id);
    } catch (error) {
      setMessage(`重试失败：${error instanceof Error ? error.message : String(error)}`);
      await refreshJob(id);
    }
  }

  async function openOutputDir(id: string) {
    try {
      await api(`/api/jobs/${id}/open-output-dir`, { method: 'POST' });
      setMessage('已打开产物目录');
    } catch (error) {
      setMessage(`打开目录失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }

  async function exportForVideoAutomation(job: Job) {
    setExportBusy(true);
    setVideoAutomationExportPath('');
    setMessage('正在导出给 VideoAutomation 使用的轻量包...');
    try {
      const result = await api<VideoAutomationExportResult>(`/api/jobs/${job.id}/export-videoautomation`, { method: 'POST' });
      setVideoAutomationExportPath(result.path);
      setMessage(`质量核验通过，已导出 ${result.validation.validVideoCount} 个视频的起号基底包：${result.path}`);
      await refreshJob(job.id);
    } catch (error) {
      setMessage(`导出失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setExportBusy(false);
    }
  }

  async function refreshPlatformMetadata(job: Job) {
    setMetadataRefreshBusy(true);
    setMessage('正在从平台补抓发布时间和视频时长，不会重跑炼化...');
    try {
      const result = await api<{ updated: number; remaining: number; errors: string[] }>(
        `/api/jobs/${job.id}/refresh-platform-metadata`,
        { method: 'POST' },
      );
      const failed = result.errors.length ? `，${result.errors.length} 条读取失败` : '';
      setMessage(`平台元数据补抓完成：更新 ${result.updated} 条，仍缺失 ${result.remaining} 条${failed}`);
      await refreshJob(job.id);
    } catch (error) {
      setMessage(`平台元数据补抓失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setMetadataRefreshBusy(false);
    }
  }

  async function cleanupJobArtifacts(job: Job) {
    if (!selectedCleanupCategories.length) {
      setMessage('请先勾选要删除的产物类别');
      return;
    }
    const labels = CLEANUP_OPTIONS.filter((item) => selectedCleanupCategories.includes(item.key)).map((item) => item.label);
    if (!window.confirm(`确定删除该任务的这些产物？\n${labels.join('、')}\n\n最终文档、manifest 和进度文件不会删除。`)) return;
    setCleanupBusy(true);
    setMessage('正在清理产物...');
    try {
      const result = await api<{ deleted_count: number; freed_bytes: number }>(`/api/jobs/${job.id}/cleanup`, {
        method: 'POST',
        body: JSON.stringify({ categories: selectedCleanupCategories }),
      });
      setMessage(`已删除 ${result.deleted_count} 项，释放 ${formatBytes(result.freed_bytes)}`);
      setCleanupSelection(emptyCleanupSelection());
      await refreshJob(job.id);
    } catch (error) {
      setMessage(`清理失败：${error instanceof Error ? error.message : String(error)}`);
      await refreshJob(job.id);
    } finally {
      setCleanupBusy(false);
    }
  }

  async function loadEvidenceTimeline(job: Job, videoId: string) {
    setEvidenceLoading(true);
    setEvidenceTimeline(null);
    try {
      const timeline = await api<EvidenceTimeline>(`/api/jobs/${job.id}/evidence/${encodeURIComponent(videoId)}`);
      if (selectedJobIdRef.current === job.id) setEvidenceTimeline(timeline);
    } catch (error) {
      setMessage(`读取证据时间线失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setEvidenceLoading(false);
    }
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">炼</span>
          <span>
            <strong>视频炼化</strong>
            <small>Local AI Refinery</small>
          </span>
        </div>
        <button className={tab === 'home' ? 'active' : ''} onClick={() => setTab('home')}>
          <Home size={18} /> 首页
        </button>
        <button className={tab === 'run' ? 'active' : ''} onClick={() => setTab('run')}>
          <Play size={18} /> 炼化
        </button>
        <button className={tab === 'jobs' ? 'active' : ''} onClick={() => setTab('jobs')}>
          <Activity size={18} /> 任务
        </button>
        <button className={tab === 'models' ? 'active' : ''} onClick={() => setTab('models')}>
          <Settings size={18} /> 设置
        </button>
      </aside>

      <main>
        <header className="topbar">
          <div>
            <span className="eyebrow">本机 Web 控制台</span>
            <h1>{tab === 'home' ? '流程配置' : tab === 'run' ? '新建炼化任务' : tab === 'jobs' ? '任务中心' : '设置'}</h1>
          </div>
          <div className="topbar-actions">
            <button onClick={runPreflight} disabled={preflightBusy}>
              <ClipboardList size={18} /> {preflightBusy ? '预检中...' : '预检'}
            </button>
            <button onClick={loadAll}>
              <RefreshCcw size={18} /> 刷新
            </button>
          </div>
        </header>

        {message && <div className="notice">{message}</div>}

        {preflight && (
          <section className="band">
            <h2>环境预检</h2>
            <div className="checks">
              {preflight.checks.map((item) => (
                <div className={`check ${item.ok ? 'ok' : item.required ? 'bad' : 'warn'}`} key={item.name}>
                  <span>{item.ok ? '通过' : item.required ? '失败' : '可选'}</span>
                  <strong>{item.name}</strong>
                  <small>{item.detail}</small>
                </div>
              ))}
            </div>
          </section>
        )}

        {tab === 'home' && (
          <section className="home-dashboard" aria-label="视频炼化流程配置">
            <div className="hero-panel">
              <div>
                <span className="eyebrow">Fixed Pipeline</span>
                <h2>把视频炼化固定成可重复的软件流程</h2>
                <p>LLM 只参与 5 个单视频维度蒸馏、跨视频合并精炼和 Benchmark Intelligence 汇总，流程判断、下载、抽帧、转写、重试全部由本机程序按状态机执行。</p>
              </div>
              <div className="hero-actions">
                <button className="primary" onClick={() => setTab('run')}>
                  <Play size={18} /> 新建任务
                </button>
                <button onClick={() => setTab('models')}>
                  <Settings size={18} /> 模型设置
                </button>
              </div>
            </div>

            <div className="metric-grid">
              <div className="metric-card">
                <span className="metric-icon blue"><Cpu size={20} /></span>
                <small>可用模型配置</small>
                <strong>{testedProfiles.length}</strong>
                <span>已测试且支持图片输入</span>
              </div>
              <div className="metric-card">
                <span className="metric-icon green"><Activity size={20} /></span>
                <small>运行中任务</small>
                <strong>{runningJobs}</strong>
                <span>队列和后台任务</span>
              </div>
              <div className="metric-card">
                <span className="metric-icon violet"><Video size={20} /></span>
                <small>任务记录</small>
                <strong>{jobs.length}</strong>
                <span>历史炼化项目</span>
              </div>
              <div className="metric-card">
                <span className="metric-icon amber"><ShieldCheck size={20} /></span>
                <small>预检状态</small>
                <strong>{preflight?.ok ? '通过' : '待检查'}</strong>
                <span>本机依赖与工具链</span>
              </div>
            </div>

            <div className="dashboard-grid">
              <section className="panel workflow-panel">
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">主流程</span>
                    <h2>6 维炼化状态机</h2>
                  </div>
                  <span className="pill ok">固定执行</span>
                </div>
                <div className="workflow-steps">
                  {PIPELINE_STEPS.map(([index, title, description]) => (
                    <div className="workflow-step" key={index}>
                      <span>{index}</span>
                      <div>
                        <strong>{title}</strong>
                        <small>{description}</small>
                      </div>
                    </div>
                  ))}
                </div>
              </section>

              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">快速配置</span>
                    <h2>任务启动前确认</h2>
                  </div>
                </div>
                <div className="config-list">
                  <div>
                    <Layers3 size={18} />
                    <span>
                      <strong>输入模式</strong>
                      <small>单视频、批量链接、博主主页</small>
                    </span>
                  </div>
                  <div>
                    <Database size={18} />
                    <span>
                      <strong>输出目录</strong>
                      <small>博主主页默认按博主名固定归档</small>
                    </span>
                  </div>
                  <div>
                    <Boxes size={18} />
                    <span>
                      <strong>文案策略</strong>
                      <small>软字幕优先；底部硬字幕足够时 OCR 可作主文案；Whisper 失败自动降级</small>
                    </span>
                  </div>
                  <div>
                    <Gauge size={18} />
                    <span>
                      <strong>失败处理</strong>
                      <small>明确状态、自动重试、保留手动重试入口</small>
                    </span>
                  </div>
                </div>
                <button className="primary full" onClick={() => setTab('run')}>
                  <Play size={18} /> 进入炼化配置
                </button>
              </section>
            </div>

            <div className="grid two">
              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">最近任务</span>
                    <h2>运行概览</h2>
                  </div>
                  <BarChart3 size={20} />
                </div>
                {latestJob ? (
                  <div className="latest-job">
                    <span className="pill">{latestJob.status}</span>
                    <strong title={latestJob.output_dir}>{displayJobName(latestJob.output_dir)}</strong>
                    <small>{latestJob.created_at}</small>
                    <button
                    onClick={() => {
                      selectJob(latestJob.id);
                      setTab('jobs');
                    }}
                    >
                      <Activity size={16} /> 查看任务
                    </button>
                  </div>
                ) : (
                  <div className="empty">还没有任务记录</div>
                )}
              </section>

              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">知识库分支</span>
                    <h2>暂未开启</h2>
                  </div>
                </div>
                <p className="muted-text">首页先只承载视频炼化主流程。知识库提炼和同步功能等对应分支接入后，再单独加入导航和配置。</p>
              </section>
            </div>
          </section>
        )}

        {tab === 'models' && (
          <section className="grid two">
            <div className="panel">
              <h2>模型供应商</h2>
              <label>
                预设
                <select value={selectedPreset} onChange={(event) => setSelectedPreset(event.target.value)}>
                  {presets.map((preset) => (
                    <option value={preset.key} key={preset.key}>
                      {preset.provider_name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                供应商名称
                <input value={profileForm.provider_name} onChange={(e) => setProfileForm({ ...profileForm, provider_name: e.target.value })} />
              </label>
              <label>
                Base URL
                <input value={profileForm.base_url} onChange={(e) => setProfileForm({ ...profileForm, base_url: e.target.value })} />
              </label>
              <label>
                API Key
                <input
                  type="password"
                  value={profileForm.api_key}
                  onChange={(e) => setProfileForm({ ...profileForm, api_key: e.target.value })}
                  placeholder="保存后不会回显"
                />
              </label>
              <div className="grid two compact">
                <label>
                  蒸馏模型
                  <input value={profileForm.analysis_model} onChange={(e) => setProfileForm({ ...profileForm, analysis_model: e.target.value })} />
                </label>
                <label>
                  合并模型
                  <input value={profileForm.merge_model} onChange={(e) => setProfileForm({ ...profileForm, merge_model: e.target.value })} />
                </label>
              </div>
              <div className="grid two compact">
                <label>
                  Max Tokens
                  <input
                    type="number"
                    value={profileForm.max_tokens}
                    onChange={(e) => setProfileForm({ ...profileForm, max_tokens: Number(e.target.value) })}
                  />
                </label>
                <label>
                  Temperature
                  <input
                    type="number"
                    step="0.1"
                    value={profileForm.temperature}
                    onChange={(e) => setProfileForm({ ...profileForm, temperature: Number(e.target.value) })}
                  />
                </label>
              </div>
              <div className="toggles">
                <label>
                  <input
                    type="checkbox"
                    checked={profileForm.supports_vision}
                    onChange={(e) => setProfileForm({ ...profileForm, supports_vision: e.target.checked })}
                  />
                  支持图片输入
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={profileForm.supports_reasoning}
                    onChange={(e) => setProfileForm({ ...profileForm, supports_reasoning: e.target.checked })}
                  />
                  支持 reasoning
                </label>
              </div>
              <button className="primary" onClick={saveProfile}>
                <Save size={18} /> 保存配置
              </button>
            </div>

            <div className="panel">
              <h2>已保存配置</h2>
              <div className="list">
                {profiles.map((profile) => (
                  <div className="item" key={profile.id}>
                    <div>
                      <strong>{profile.provider_name}</strong>
                      <small>{profile.base_url}</small>
                      <small>
                        {profile.analysis_model} / {profile.merge_model}
                      </small>
                    </div>
                    <span className={profile.is_tested && profile.supports_vision ? 'pill ok' : 'pill'}>{profile.is_tested ? '已测试' : '未测试'}</span>
                    <button disabled={testingProfileId === profile.id} onClick={() => testProfile(profile.id)}>
                      <TestTube2 size={16} /> {testingProfileId === profile.id ? '测试中' : '测试'}
                    </button>
                    <button
                      className="danger"
                      disabled={deletingProfileId === profile.id}
                      onClick={() => deleteProfile(profile.id, profile.provider_name)}
                    >
                      <Trash2 size={16} /> 删除
                    </button>
                  </div>
                ))}
              </div>
            </div>
          </section>
        )}

        {tab === 'run' && (
          <section className="panel wide">
            <h2>新建炼化任务</h2>
            <div className="grid two">
              <label>
                输入类型
                <select value={jobForm.input_type} onChange={(e) => setJobForm({ ...jobForm, input_type: e.target.value })}>
                  <option value="single">单视频</option>
                  <option value="batch">批量链接</option>
                  <option value="blogger">博主主页</option>
                </select>
              </label>
              <label>
                模型配置
                <select value={jobForm.model_profile_id} onChange={(e) => setJobForm({ ...jobForm, model_profile_id: e.target.value })}>
                  <option value="">选择已测试配置</option>
                  {testedProfiles.map((profile) => (
                    <option value={profile.id} key={profile.id}>
                      {profile.provider_name} · {profile.analysis_model}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label>
              输入
              <textarea
                rows={10}
                value={jobForm.inputs}
                onChange={(e) => setJobForm({ ...jobForm, inputs: e.target.value })}
                placeholder="每行一个视频链接、视频 ID 或博主主页 URL"
              />
            </label>
            <div className="grid two">
              <label>
                输出目录
                <input
                  value={jobForm.output_dir}
                  onChange={(e) => setJobForm({ ...jobForm, output_dir: e.target.value })}
                  placeholder="留空：博主主页按博主名固定目录"
                />
              </label>
              <label>
                最大视频数
                <input type="number" value={jobForm.max_videos} onChange={(e) => setJobForm({ ...jobForm, max_videos: Number(e.target.value) })} />
              </label>
            </div>
            <div className="frame-settings-panel">
              <div className="frame-settings-head">
                <div>
                  <h3>帧图抽取密度</h3>
                  <p>默认配置会按视频时长自动抽帧：10 分钟内每 1 秒 1 帧，超过 10 分钟每 5 秒 1 帧。拖动滑动条后才启用自定义间隔。</p>
                </div>
                <button
                  type="button"
                  onClick={() =>
                    setJobForm({
                      ...jobForm,
                      frame_interval_seconds: CUSTOM_FRAME_INTERVAL_DEFAULT_SECONDS,
                      frame_interval_custom: false,
                    })
                  }
                >
                  默认配置
                </button>
              </div>
              <label className="range-label">
                <span>
                  当前：
                  <strong>
                    {jobForm.frame_interval_custom
                      ? `自定义，每 ${jobForm.frame_interval_seconds} 秒 1 帧`
                      : '默认配置：10 分钟内 1 秒/帧，10 分钟外 5 秒/帧'}
                  </strong>
                </span>
                <input
                  className={jobForm.frame_interval_custom ? 'active' : 'inactive'}
                  type="range"
                  min="1"
                  max="30"
                  step="1"
                  value={jobForm.frame_interval_seconds}
                  onPointerDown={() => setJobForm({ ...jobForm, frame_interval_custom: true })}
                  onKeyDown={() => setJobForm({ ...jobForm, frame_interval_custom: true })}
                  onChange={(event) =>
                    setJobForm({
                      ...jobForm,
                      frame_interval_seconds: Number(event.target.value),
                      frame_interval_custom: true,
                    })
                  }
                />
              </label>
              <div className="frame-estimates">
                {FRAME_ESTIMATE_DURATIONS.map((minutes) => {
                  const estimate = frameEstimate(minutes, jobForm.frame_interval_custom, jobForm.frame_interval_seconds);
                  return (
                    <div className="frame-estimate-card" key={minutes}>
                      <strong>{minutes} 分钟视频</strong>
                      <small>{estimate.intervalSeconds} 秒/帧</small>
                      <span>{estimate.frameCount.toLocaleString('zh-CN')} 帧</span>
                      <small>{formatBytes(estimate.storageBytes)} 预估容量</small>
                      <small>模型图 {estimate.modelFrameCount} 张</small>
                      <small>炼化 {formatDurationEstimate(estimate.modelTimeSeconds)}</small>
                    </div>
                  );
                })}
              </div>
              <p className="frame-estimate-note">
                时间是这些帧经过筛选后参与 5 个单视频图文维度炼化的粗略预估，不包含 Benchmark Intelligence 汇总，不是 ffmpeg 抽帧耗时。实际耗时会受模型速度、网络和图片复杂度影响。
              </p>
            </div>
            <button className="primary" disabled={!jobForm.model_profile_id} onClick={createJob}>
              <Play size={18} /> 启动固定流程
            </button>
          </section>
        )}

        {tab === 'jobs' && (
          <section className="jobs-layout">
            <div className="panel job-actions-panel">
              <h2>任务详情</h2>
              {activeJob ? (
                <>
                  <div className="actions">
                    <button onClick={() => refreshJob(activeJob.id)}>
                      <RefreshCcw size={16} /> 刷新
                    </button>
                    <button onClick={() => cancelJob(activeJob.id)}>
                      <Square size={16} /> 取消
                    </button>
                    <button disabled={['queued', 'running'].includes(activeJob.status)} onClick={() => retryJob(activeJob.id)}>
                      <Play size={16} /> {['queued', 'running'].includes(activeJob.status) ? '运行中' : '重试'}
                    </button>
                    <button onClick={() => openOutputDir(activeJob.id)}>
                      <FolderOpen size={16} /> 产物目录
                    </button>
                    <button
                      disabled={metadataRefreshBusy || ['queued', 'running'].includes(activeJob.status)}
                      title="重新从平台读取缺失的发布时间和视频时长，不会重跑炼化"
                      onClick={() => refreshPlatformMetadata(activeJob)}
                    >
                      <RefreshCcw size={16} /> {metadataRefreshBusy ? '补抓中' : '补抓发布时间'}
                    </button>
                    <button
                      disabled={exportBusy || !activeJobCanExport}
                      title={activeJobCanExport ? '导出给 VideoAutomation 使用' : '任务完成后才能导出'}
                      onClick={() => exportForVideoAutomation(activeJob)}
                    >
                      <Boxes size={16} /> {exportBusy ? '导出中' : '导出给 VideoAutomation 使用'}
                    </button>
                  </div>
                  {videoAutomationExportPath && (
                    <div className="export-result">
                      <strong>VideoAutomation 导出路径</strong>
                      <code>{videoAutomationExportPath}</code>
                    </div>
                  )}
                  {activeJob.error && (
                    <div className="task-warning">
                      <strong>{['queued', 'running'].includes(activeJob.status) ? '等待云端模型自动重试' : '任务错误'}</strong>
                      <span>{activeJob.error}</span>
                    </div>
                  )}
                  <div className="cleanup-panel">
                    <div className="cleanup-head">
                      <strong>清理产物</strong>
                      <small>只清理勾选类别，最终文档、Benchmark 产物、manifest 和进度文件会保留</small>
                    </div>
                    <div className="cleanup-options">
                      {CLEANUP_OPTIONS.map((item) => {
                        const sizeInfo = activeJob.cleanup_sizes?.[item.key];
                        return (
                          <label className="cleanup-option" key={item.key}>
                            <input
                              type="checkbox"
                              checked={cleanupSelection[item.key]}
                              onChange={(event) => setCleanupSelection((prev) => ({ ...prev, [item.key]: event.target.checked }))}
                            />
                            <span>
                              <span className="cleanup-title">
                                <strong>{item.label}</strong>
                                <small>{formatBytes(sizeInfo?.bytes || 0)}</small>
                              </span>
                              <small>{item.description}</small>
                            </span>
                          </label>
                        );
                      })}
                    </div>
                    <button
                      className="danger"
                      disabled={cleanupBusy || ['queued', 'running'].includes(activeJob.status) || selectedCleanupCategories.length === 0}
                      onClick={() => cleanupJobArtifacts(activeJob)}
                    >
                      <Trash2 size={16} /> {cleanupBusy ? '删除中' : '一键删除'}
                    </button>
                  </div>
                </>
              ) : (
                <div className="empty">选择一个任务</div>
              )}
            </div>
            <aside className="panel job-list-panel">
              <h2>任务列表</h2>
              <div className="list">
                {taskListJobs.map((job) => (
                  <button
                    className={`job-row ${activeJob?.id === job.id ? 'active' : ''}`}
                    key={job.id}
                    title={job.output_dir}
                    onClick={() => selectJob(job.id)}
                  >
                    <span className="job-name">{displayJobName(job.output_dir)}</span>
                  </button>
                ))}
              </div>
            </aside>
            <div className="panel job-dashboard-panel">
              <h2>任务仪表盘</h2>
              {activeJob ? (
                <>
                  {(() => {
                    const progress = computeJobProgress(activeJob, logs);
                    return (
                      <div className="progress-panel">
                        <div className="progress-head">
                          <strong>总进度 {progress.percent}%</strong>
                          <span>
                            {progress.terminalCount}/{progress.totalVideos || 0} 视频已结束
                          </span>
                        </div>
                        <div className="progress-track">
                          <div className="progress-fill" style={{ width: `${progress.percent}%` }} />
                        </div>
                        <div className="progress-meta">
                          <span>当前：{progress.currentLabel}</span>
                          <span>成功 {progress.doneCount}</span>
                          <span>失败 {progress.failedCount}</span>
                          <span>等待重试 {progress.retryWaitCount}</span>
                          <span>跳过 {progress.skippedCount}</span>
                        </div>
                      </div>
                    );
                  })()}

                  <h3>视频进度</h3>
                  <div className="video-card-grid">
                    {(activeJob.videos || []).map((video) => {
                      const videoProgress = computeVideoProgress(
                        video,
                        activeJob.dimensions || [],
                        ['queued', 'running'].includes(activeJob.status),
                      );
                      const title = displayVideoTitle(video);
                      const message = videoStatusMessage(video);
                      return (
                        <div className={`video-card status-${statusClass(String(video.status))}`} key={video.id}>
                          <div className="video-card-head">
                            <strong title={String(video.video_id)}>{video.video_id}</strong>
                            <span className="pill">{displayVideoStatus(video.status)}</span>
                          </div>
                          <small title={title}>{title}</small>
                          <div className="mini-progress">
                            <div style={{ width: `${videoProgress}%` }} />
                          </div>
                          <div className="video-card-foot">
                            <span>完成进度 {videoProgress}%</span>
                            {message && <span title={message}>{message}</span>}
                          </div>
                          {video.status === 'done' && (
                            <button
                              className="icon-button video-evidence-button"
                              title="查看证据时间线"
                              aria-label={`查看 ${video.video_id} 的证据时间线`}
                              onClick={() => loadEvidenceTimeline(activeJob, String(video.video_id))}
                            >
                              <Eye size={16} />
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>

                  <section className="evidence-timeline" aria-live="polite">
                    <div className="evidence-heading">
                      <div>
                        <h3>证据时间线</h3>
                        <p>关键画面、对应文案与视觉结论均可回溯；证据窗口只是采样分析范围，不等于真实镜头或精确切点。</p>
                      </div>
                      {evidenceLoading && <span className="pill">加载中</span>}
                    </div>
                    {evidenceTimeline ? (
                      <>
                        <div className="evidence-summary">
                          <span>视频 ID：{evidenceTimeline.video_id}</span>
                          <span>时长：{Math.round(evidenceTimeline.duration_seconds || 0)} 秒</span>
                          <span>时间线节点：{evidenceTimeline.shots.length}</span>
                          <span>检测切段：{evidenceTimeline.shots.filter((shot) => shot.segment_type === 'detected_cut_segment').length}</span>
                          <span>文案对齐：{String(evidenceTimeline.quality?.alignment_status || evidenceTimeline.quality?.transcript_alignment || 'unknown')}</span>
                          <span>精确时序：{evidenceTimeline.quality?.eligible_for_precise_timing ? '可用' : '不适用'}</span>
                        </div>
                        {!!evidenceTimeline.scene_curve?.length && (
                          <div className="scene-curve" title="本地 FFmpeg 计算的场景变化强度；柱越高代表画面变化越明显，不代表内容质量。">
                            <span className="scene-curve-label">场景变化</span>
                            <div className="scene-curve-bars" aria-label="场景变化曲线">
                              {evidenceTimeline.scene_curve.map((point, index) => (
                                <i
                                  key={`${point.timestamp_seconds}-${index}`}
                                  style={{ height: `${Math.max(4, Math.min(100, point.score * 100))}%` }}
                                  title={`${Math.round(point.timestamp_seconds)} 秒 · 变化强度 ${point.score.toFixed(3)}`}
                                />
                              ))}
                            </div>
                          </div>
                        )}
                        <div className="evidence-shot-grid">
                          {evidenceTimeline.shots.map((shot) => (
                            <article className="evidence-shot" key={shot.evidence_id}>
                              {shot.keyframe_url ? (
                                <img src={`${API}${shot.keyframe_url}`} alt={`${shot.evidence_id} 关键帧`} loading="lazy" />
                              ) : (
                                <div className="evidence-image-empty">关键帧不可用</div>
                              )}
                              <div className="evidence-shot-meta">
                                <span>{shot.time_range}</span>
                                <span title={shot.evidence_id}>{shot.evidence_id}</span>
                              </div>
                              <p>{shot.visual_observation?.visual_description || '未生成视觉描述'}</p>
                              {shot.transcript_excerpt && <small>文案：{shot.transcript_excerpt}</small>}
                              {shot.ocr_excerpt && <small>屏幕文字：{shot.ocr_excerpt}</small>}
                              <div className="evidence-shot-tags">
                                <span>{evidenceSegmentLabel(shot.segment_type)}</span>
                                <span>{evidenceBoundaryLabel(shot.boundary_source)}</span>
                                <span>边界 {shot.boundary_confidence || 'low'}</span>
                                <span>{shot.visual_observation?.shot_type || '未确认'}</span>
                                <span>{shot.visual_observation?.confidence || 'low'}</span>
                                <span>{shot.text_alignment || 'coarse'}</span>
                              </div>
                            </article>
                          ))}
                        </div>
                      </>
                    ) : (
                      <div className="empty">点击已完成视频卡片右下角的查看按钮，加载该视频的证据时间线。</div>
                    )}
                  </section>

                  <div className="dashboard-lower-grid">
                    <div className="side-stack">
                      <div>
                        <h3>产物</h3>
                        <div className="artifacts">
                          {visibleArtifacts(activeJob).map((item) => (
                            <a href={`${API}/api/files?path=${encodeURIComponent(item.path)}`} target="_blank" key={item.id}>
                              <FileText size={16} /> {artifactDisplayName(item)}
                            </a>
                          ))}
                        </div>
                      </div>
                      <div>
                        <h3>日志</h3>
                        <pre className="logs">
                          {logs.map((line) => `[${line.level}] ${line.message}`).join('\n')}
                        </pre>
                      </div>
                    </div>
                  </div>
                </>
              ) : (
                <div className="empty">选择一个任务后显示总进度和视频仪表盘</div>
              )}
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
