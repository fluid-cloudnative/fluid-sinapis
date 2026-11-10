import { ref } from 'vue'

/**
 * Minimal i18n for the web console.
 *
 * English is the default language; the visitor's choice is kept in localStorage.
 * Metric labels / helps and section titles are looked up by the metric name and
 * section key the backend already sends; anything missing falls back to the
 * backend-provided display fields, so a new collector metric still renders
 * (untranslated) instead of disappearing.
 *
 * When adding a metric to server/collectors, add its name here too.
 */

const STORAGE_KEY = 'sinapis.lang'
const DEFAULT_LANG = 'en'

export const LANGUAGES = [
  { value: 'en', label: 'EN' },
  { value: 'zh', label: '中文' },
]

const STRINGS = {
  en: {
    docTitle: 'Fluid-Sinapis · Cache Console',
    appTagline: 'Intelligent cache console',
    langToggle: 'Switch language',

    autoRefresh: 'Auto refresh',
    refreshOff: 'Off',
    refresh5s: '5s',
    refresh10s: '10s',
    refresh30s: '30s',
    refresh: 'Refresh',

    metricsUnavailable: 'Failed to load metrics',
    apiError: 'API {path} returned {status}',
    retry: 'Retry',
    requestFailed: 'Request failed',
    backendDown: 'Backend error',
    liveCollection: 'Live collection',
    updatedAt: 'Updated {time}',
    refreshing: 'Refreshing…',
    noData: 'No data yet',
    reload: 'Reload',
    dataSource: 'Data source: server/collectors (FluidCollector / K8sCollector)',
    apiEndpoints: 'API /api/overview · /api/cached-files',

    apiKeyRequired: 'API key required or invalid',
    apiKeyTitle: 'API key',
    apiKeyPrompt: 'This console requires the API key configured on the backend (SINAPIS_API_KEY). It is stored in this browser only.',
    apiKeyPlaceholder: 'Paste the API key',
    apiKeySave: 'Save and reload',
    apiKeyWrong: 'The key was rejected (401). Check it and try again.',

    fluidCaption: 'Used cache ÷ total capacity',
    k8sCaption: 'Scheduled memory ÷ allocatable memory',
    metricCount: '{n} metrics',
    collectedAt: 'collected at {time}',
    collectFailed: 'Collection failed',
    badgeError: 'Error',
    badgeLive: 'Live',
    collectFailedTitle: 'Failed to collect metrics',
    hintKubeBefore: 'Make sure',
    hintKubeAfter: 'points to a usable context in your kubeconfig',
    hintRbac: 'Ensure the cluster API server is reachable and RBAC allows reading Runtime / Node / Pod',
    rawMetrics: 'Raw metrics',
    thMetric: 'Metric',
    thValue: 'Value',
    thLabels: 'Labels',
    usage: 'Usage',

    cachedFiles: 'Cached files',
    filesSummary: '{n} files · {size} total',
    scanPath: 'scanning {path}',
    cachedFilesErrorTitle: 'Failed to list cached files',
    hintAlluxio: 'Ensure kube_context is reachable and the Alluxio master Pod is running',
    hintAlluxioScan: 'The list comes from alluxio fs ls; each scan takes seconds and is cached for 30s',
    thIndex: '#',
    thFile: 'File',
    thSize: 'Size',
    thCachedPct: 'Cached',
    thCachedAt: 'Cached at',
    filesEmpty: 'No files currently cached (cache ratio > 0%)',
    filesEmptyHint: 'Files cached by preloading or read operations will appear here',
    filesNote: 'Cached-at is when the file first entered the cache (ratio > 0%); the console backend tracks it continuously',

    derivedTag: 'derived',
  },

  zh: {
    docTitle: 'Fluid-Sinapis · 智能缓存控制台',
    appTagline: '智能缓存控制台',
    langToggle: '切换语言',

    autoRefresh: '自动刷新',
    refreshOff: '关闭',
    refresh5s: '5 秒',
    refresh10s: '10 秒',
    refresh30s: '30 秒',
    refresh: '刷新',

    metricsUnavailable: '无法获取指标',
    apiError: '接口 {path} 返回 {status}',
    retry: '重试',
    requestFailed: '请求失败',
    backendDown: '后端异常',
    liveCollection: '实时采集',
    updatedAt: '更新于 {time}',
    refreshing: '刷新中…',
    noData: '暂无数据',
    reload: '重新加载',
    dataSource: '数据来源：server/collectors（FluidCollector / K8sCollector）',
    apiEndpoints: '接口 /api/overview · /api/cached-files',

    apiKeyRequired: 'API Key 缺失或无效',
    apiKeyTitle: 'API Key',
    apiKeyPrompt: '本控制台需要后端配置的 API Key（SINAPIS_API_KEY）。Key 只保存在本浏览器中。',
    apiKeyPlaceholder: '粘贴 API Key',
    apiKeySave: '保存并加载',
    apiKeyWrong: 'Key 未通过校验（401），请检查后重试。',

    fluidCaption: '已用缓存 ÷ 缓存总容量',
    k8sCaption: '已调度内存 ÷ 可分配内存',
    metricCount: '{n} 项指标',
    collectedAt: '采集于 {time}',
    collectFailed: '采集失败',
    badgeError: '异常',
    badgeLive: '实时',
    collectFailedTitle: '指标采集失败',
    hintKubeBefore: '确认',
    hintKubeAfter: '指向 kubeconfig 中可用的 context',
    hintRbac: '确认集群 API Server 可达，且 RBAC 允许读取 Runtime / Node / Pod',
    rawMetrics: '查看原始指标',
    thMetric: '指标名',
    thValue: '数值',
    thLabels: '标签',
    usage: '占用',

    cachedFiles: '已缓存文件',
    filesSummary: '{n} 个文件 · 共 {size}',
    scanPath: '扫描 {path}',
    cachedFilesErrorTitle: '缓存文件列表获取失败',
    hintAlluxio: '确认 kube_context 可达，且 Alluxio master Pod 正在运行',
    hintAlluxioScan: '缓存文件列表来自 alluxio fs ls，扫描一次约需数秒，已按 30 秒缓存',
    thIndex: '序号',
    thFile: '文件名称',
    thSize: '文件大小',
    thCachedPct: '缓存比例',
    thCachedAt: '缓存时间',
    filesEmpty: '当前没有缓存中的文件（缓存比例 > 0%）',
    filesEmptyHint: '通过预加载或读操作产生的缓存文件会出现在这里',
    filesNote: '缓存时间为该文件首次进入缓存（缓存比例 > 0%）的时刻，由控制台后端持续跟踪记录',

    derivedTag: '推导',
  },
}

// -- Metric display fields, keyed by the metric name the backend sends ------- //
const METRIC_LABELS = {
  en: {
    k8s_nodes: 'Cluster nodes',
    k8s_node_allocatable_memory_bytes: 'Allocatable memory',
    k8s_free_memory_bytes: 'Free memory',
    k8s_ready_pods: 'Ready pods',
    fluid_cache_capacity_bytes: 'Total cache capacity',
    fluid_cache_used_bytes: 'Used cache',
    fluid_cache_used_ratio: 'Cache usage',
    fluid_cache_capacity_per_worker_bytes: 'Avg per-worker capacity',
    fluid_workers: 'Ready workers',
    fluid_workers_desired: 'Configured workers',
    derived_memory_allocated_ratio: 'Memory allocation',
  },
  zh: {
    k8s_nodes: '集群节点数',
    k8s_node_allocatable_memory_bytes: '可分配内存总量',
    k8s_free_memory_bytes: '空闲内存',
    k8s_ready_pods: 'Ready Pod 数',
    fluid_cache_capacity_bytes: '缓存总容量',
    fluid_cache_used_bytes: '已用缓存',
    fluid_cache_used_ratio: '缓存使用率',
    fluid_cache_capacity_per_worker_bytes: '单 Worker 平均容量',
    fluid_workers: 'Ready Worker 数',
    fluid_workers_desired: '配置 Worker 副本数',
    derived_memory_allocated_ratio: '内存分配率',
  },
}

// English helps mirror the collectors; zh translates them. Unknown metrics fall
// back to the help text the backend already sends.
const METRIC_HELPS = {
  en: {
    k8s_nodes: 'Number of nodes in the cluster (all roles are counted; the node_role label is informational only).',
    k8s_node_allocatable_memory_bytes: 'Sum of allocatable memory across all cluster nodes, in bytes (not filtered by role).',
    k8s_ready_pods: 'Number of Ready pods in the target namespace.',
    k8s_free_memory_bytes: "Estimated free memory: allocatable minus the memory requests of the target namespace's pods (other namespaces are not deducted, so the value overstates true free memory; in usage mode, allocatable minus actual working set).",
    fluid_cache_capacity_bytes: 'Total Fluid cache capacity in bytes.',
    fluid_cache_capacity_per_worker_bytes: 'Average Fluid cache capacity per ready worker, in bytes.',
    fluid_workers: 'Number of ready Fluid worker replicas.',
    fluid_workers_desired: 'Configured Fluid worker replica count (spec.replicas).',
    fluid_cache_used_bytes: 'Fluid cache currently in use, in bytes.',
    fluid_cache_used_ratio: 'Fraction of the Fluid cache in use (0..1).',
    derived_memory_allocated_ratio: 'Scheduled memory as a fraction of allocatable memory (0..1).',
  },
  zh: {
    k8s_nodes: '集群节点总数（统计所有角色，node_role 标签仅供参考）。',
    k8s_node_allocatable_memory_bytes: '所有集群节点可分配内存之和（字节，不按角色过滤）。',
    k8s_ready_pods: '目标命名空间中 Ready 的 Pod 数量。',
    k8s_free_memory_bytes: '估算空闲内存：可分配内存减去目标命名空间 Pod 的内存 request（不扣除其他命名空间，数值偏高；usage 模式下为可分配内存减去实际 working set）。',
    fluid_cache_capacity_bytes: 'Fluid 缓存总容量（字节）。',
    fluid_cache_capacity_per_worker_bytes: '平均每个 Ready Worker 的缓存容量（字节）。',
    fluid_workers: '处于 Ready 状态的 Fluid worker 副本数。',
    fluid_workers_desired: '配置的 Fluid worker 副本数（spec.replicas）。',
    fluid_cache_used_bytes: '当前已使用的 Fluid 缓存（字节）。',
    fluid_cache_used_ratio: 'Fluid 缓存使用比例（0..1）。',
    derived_memory_allocated_ratio: '已调度内存占可分配内存的比例（0..1）。',
  },
}

const SECTION_TITLES = {
  en: { k8s: 'K8s cluster', fluid: 'Fluid system' },
  zh: { k8s: 'K8s 集群信息', fluid: 'Fluid 系统信息' },
}

function readStoredLang() {
  // localStorage can be unavailable (private mode, blocked storage); default to English
  try {
    return localStorage.getItem(STORAGE_KEY) === 'zh' ? 'zh' : DEFAULT_LANG
  } catch {
    return DEFAULT_LANG
  }
}

export const lang = ref(readStoredLang())

/** Locale tag for number / date formatting that follows the UI language. */
export function localeTag() {
  return lang.value === 'zh' ? 'zh-CN' : 'en-US'
}

function applyDocument() {
  document.documentElement.lang = lang.value === 'zh' ? 'zh-CN' : 'en'
  document.title = t('docTitle')
}

export function setLang(next) {
  if (!STRINGS[next]) return
  lang.value = next
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    /* storage unavailable: the choice just won't persist */
  }
  applyDocument()
}

/** Look up a UI string; {name} placeholders are filled from params. */
export function t(key, params) {
  const table = STRINGS[lang.value] ?? STRINGS[DEFAULT_LANG]
  const template = table[key] ?? STRINGS[DEFAULT_LANG][key] ?? key
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match)
}

export function metricLabel(name, fallback) {
  return METRIC_LABELS[lang.value][name] ?? fallback ?? name
}

export function metricHelp(name, fallback) {
  return METRIC_HELPS[lang.value][name] ?? fallback ?? ''
}

export function sectionTitle(key, fallback) {
  return SECTION_TITLES[lang.value][key] ?? fallback ?? key
}

applyDocument()
