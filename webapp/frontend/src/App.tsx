import { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  BarChart3,
  Boxes,
  ClipboardList,
  Cpu,
  Database,
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

const CLEANUP_OPTIONS: Array<{ key: CleanupCategory; label: string; description: string }> = [
  { key: 'frames', label: '帧图片', description: '原始数据里的 *_frames 目录' },
  { key: 'single_analysis', label: '单视频分析', description: '单视频分析目录' },
  { key: 'kept_videos', label: '视频保留', description: '视频保留目录' },
  { key: 'transcripts', label: '文案', description: '文案目录' },
  { key: 'raw_data', label: '原始数据', description: '下载视频和转写中间文件' },
];

const PIPELINE_STEPS = [
  ['01', '环境预检', '确认 ffmpeg、Whisper、OCR、下载组件和模型配置可用。'],
  ['02', '解析与下载', '解析单视频、批量链接或博主主页，按阶梯方案下载视频。'],
  ['03', '抽帧与文案', '固定抽帧；文案优先软字幕，其次 Whisper，OCR 只做校对辅助。'],
  ['04', '资料检查', '跳过资料不完整的视频，失败进入明确状态并支持自动重试。'],
  ['05', '5 维炼化', '单视频蒸馏后跨视频合并，产出 5 份兼容旧结构的文档。'],
];

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

function computeJobProgress(job: Job) {
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
  const percent = totalVideos ? Math.max(0, Math.min(100, Math.round((progressSum / totalUnits) * 100))) : 0;
  const currentLabel = currentVideo
    ? `${currentVideo.video_id || '当前视频'} · ${currentDimension?.dimension || currentStep}`
    : job.status === 'running'
      ? '合并精炼或收尾中'
      : job.status === 'partial_done'
        ? '已生成阶段性结果，可重试失败视频'
      : job.status === 'failed'
        ? '任务已停止，可点击重试继续'
      : '无运行中步骤';

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
    .filter((item) => ['final_output', 'manifest', 'progress'].includes(item.kind))
    .filter((item) => {
      const key = `${item.kind}:${item.path}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
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
  const [jobForm, setJobForm] = useState({
    input_type: 'batch',
    inputs: '',
    output_dir: '',
    model_profile_id: '',
    max_videos: 50,
  });
  const [message, setMessage] = useState('');

  const testedProfiles = useMemo(() => profiles.filter((item) => item.is_tested && item.supports_vision), [profiles]);
  const selectedCleanupCategories = CLEANUP_OPTIONS.filter((item) => cleanupSelection[item.key]).map((item) => item.key);
  const runningJobs = jobs.filter((job) => ['queued', 'running'].includes(job.status)).length;
  const latestJob = jobs[0];

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
      }),
    });
    setActiveJob(job);
    setTab('jobs');
    watchJob(job.id);
    await loadAll();
  }

  async function refreshJob(id: string) {
    const detail = await api<Job>(`/api/jobs/${id}`);
    setActiveJob(detail);
    await loadAll();
  }

  function watchJob(id: string) {
    setLogs([]);
    const stream = new EventSource(`${API}/api/jobs/${id}/events`);
    stream.onmessage = (event) => {
      const line = JSON.parse(event.data);
      if (line.message === '[stream-end]') {
        stream.close();
        refreshJob(id).catch(console.error);
        return;
      }
      setLogs((prev) => [...prev.slice(-250), line]);
      refreshJob(id).catch(console.error);
    };
    stream.onerror = () => stream.close();
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
                <p>LLM 只参与单视频 5 维蒸馏和跨视频合并精炼，流程判断、下载、抽帧、转写、重试全部由本机程序按状态机执行。</p>
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
                    <h2>5 维炼化状态机</h2>
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
                      <small>软字幕优先，Whisper 为主，OCR 只辅助校对</small>
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
                        refreshJob(latestJob.id).catch(console.error);
                        watchJob(latestJob.id);
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
                  </div>
                  <div className="cleanup-panel">
                    <div className="cleanup-head">
                      <strong>清理产物</strong>
                      <small>只清理勾选类别，最终 5 份文档、manifest 和进度文件会保留</small>
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
                {jobs.map((job) => (
                  <button
                    className={`job-row ${activeJob?.id === job.id ? 'active' : ''}`}
                    key={job.id}
                    title={job.output_dir}
                    onClick={() => {
                      refreshJob(job.id).catch(console.error);
                      watchJob(job.id);
                    }}
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
                    const progress = computeJobProgress(activeJob);
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
                        </div>
                      );
                    })}
                  </div>

                  <div className="dashboard-lower-grid">
                    <div className="side-stack">
                      <div>
                        <h3>产物</h3>
                        <div className="artifacts">
                          {visibleArtifacts(activeJob).map((item) => (
                            <a href={`${API}/api/files?path=${encodeURIComponent(item.path)}`} target="_blank" key={item.id}>
                              <FileText size={16} /> {item.kind} · {item.path}
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
