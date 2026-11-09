<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { fetchCachedFiles, fetchOverview } from './api'
import CachedFilesPanel from './components/CachedFilesPanel.vue'
import SectionPanel from './components/SectionPanel.vue'
import { LANGUAGES, lang, setLang, t } from './i18n'
import { formatTime } from './utils/format'

const REFRESH_OPTIONS = [
  { key: 'refreshOff', value: 0 },
  { key: 'refresh5s', value: 5000 },
  { key: 'refresh10s', value: 10000 },
  { key: 'refresh30s', value: 30000 },
]

const data = ref(null)
const error = ref('')
const cachedFiles = ref(null)
const loading = ref(false)
const interval = ref(readStoredInterval())
const firstLoad = computed(() => !data.value && loading.value)

function readStoredInterval() {
  // localStorage can be unavailable (private mode, blocked storage); refresh still works
  try {
    return Number(localStorage.getItem('sinapis.refresh-ms') ?? 10000)
  } catch {
    return 10000
  }
}

let timer = null
let controller = null

async function load() {
  if (loading.value) return
  loading.value = true
  controller = new AbortController()
  try {
    // The cached-files list failing must not blank the whole page: its error is
    // carried in its own payload and shown inside its panel only.
    const [overview, cached] = await Promise.all([
      fetchOverview(controller.signal),
      fetchCachedFiles(controller.signal).catch((err) => {
        if (err.name === 'AbortError') throw err
        return { status: 'error', error: err.message || t('requestFailed'), files: [] }
      }),
    ])
    data.value = overview
    cachedFiles.value = cached
    error.value = ''
  } catch (err) {
    if (err.name === 'AbortError') return
    error.value = err.message || t('requestFailed')
  } finally {
    loading.value = false
  }
}

function schedule() {
  if (timer) window.clearInterval(timer)
  timer = null
  if (interval.value > 0) {
    timer = window.setInterval(() => {
      if (!document.hidden) load()
    }, interval.value)
  }
}

function handleVisibility() {
  if (document.hidden || interval.value === 0) return
  const stamp = data.value?.generated_at_ms ?? 0
  if (Date.now() - stamp > interval.value) load()
}

watch(interval, (value) => {
  try {
    localStorage.setItem('sinapis.refresh-ms', String(value))
  } catch {
    /* storage unavailable: the choice just won't persist */
  }
  schedule()
})

onMounted(() => {
  load()
  schedule()
  document.addEventListener('visibilitychange', handleVisibility)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
  controller?.abort()
  document.removeEventListener('visibilitychange', handleVisibility)
})
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="header-inner">
        <div class="brand">
          <span class="brand-mark">
            <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true">
              <path
                d="M16 4c4.4 5 8 8.5 8 12.8A8 8 0 0 1 8 16.8C8 12.5 11.6 9 16 4Z"
                fill="#ffffff"
              />
            </svg>
          </span>
          <div class="brand-text">
            <h1>Fluid-Sinapis</h1>
            <p>{{ t('appTagline') }}</p>
          </div>
        </div>

        <div v-if="data?.target" class="meta-chips">
          <span class="meta-chip">
            <i>namespace</i>{{ data.target.namespace }}
          </span>
          <span class="meta-chip">
            <i>dataset</i>{{ data.target.dataset }}
          </span>
          <span class="meta-chip">
            <i>runtime</i>{{ data.target.runtime_name }}
          </span>
        </div>

        <div class="header-actions">
          <div class="lang-toggle" role="group" :aria-label="t('langToggle')">
            <button
              v-for="item in LANGUAGES"
              :key="item.value"
              type="button"
              :class="{ active: lang === item.value }"
              :aria-pressed="lang === item.value"
              @click="setLang(item.value)"
            >
              {{ item.label }}
            </button>
          </div>
          <label class="refresh-control">
            <span>{{ t('autoRefresh') }}</span>
            <select v-model.number="interval" :aria-label="t('autoRefresh')">
              <option v-for="option in REFRESH_OPTIONS" :key="option.value" :value="option.value">
                {{ t(option.key) }}
              </option>
            </select>
          </label>
          <button class="refresh-button" type="button" :disabled="loading" @click="load">
            <svg
              viewBox="0 0 24 24"
              width="15"
              height="15"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
              :class="{ spinning: loading }"
              aria-hidden="true"
            >
              <path d="M20 12a8 8 0 1 1-2.6-5.9" />
              <path d="M20 4v4.5h-4.5" />
            </svg>
            {{ t('refresh') }}
          </button>
        </div>
      </div>
    </header>

    <main class="app-main">
      <div v-if="error" class="banner banner-error" role="alert">
        <strong>{{ t('metricsUnavailable') }}</strong>
        <span>{{ error }}</span>
        <button type="button" class="banner-action" @click="load">{{ t('retry') }}</button>
      </div>

      <div class="statusbar">
        <div class="status-left">
          <span class="status-dot" :class="error ? 'is-down' : 'is-up'"></span>
          <span>{{ error ? t('backendDown') : t('liveCollection') }}</span>
          <span class="sep">·</span>
          <span>{{ t('updatedAt', { time: formatTime(data?.generated_at_ms) }) }}</span>
          <template v-if="data?.target">
            <span class="sep">·</span>
            <span class="status-context">{{ data.target.kube_context }}</span>
          </template>
        </div>
        <span v-if="loading" class="status-loading">{{ t('refreshing') }}</span>
      </div>

      <div v-if="firstLoad" class="skeleton-list">
        <div v-for="index in 2" :key="index" class="skeleton-panel">
          <div class="skeleton-line w-40"></div>
          <div class="skeleton-body">
            <div class="skeleton-block"></div>
            <div class="skeleton-grid">
              <div v-for="cell in 4" :key="cell" class="skeleton-card"></div>
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="data" class="panel-list">
        <SectionPanel
          v-for="section in data.sections"
          :key="section.key"
          :section="section"
        />
        <CachedFilesPanel :data="cachedFiles" />
      </div>

      <div v-else-if="!error" class="empty-state">
        <p>{{ t('noData') }}</p>
        <button type="button" class="refresh-button" @click="load">{{ t('reload') }}</button>
      </div>
    </main>

    <footer class="app-footer">
      <span>{{ t('dataSource') }}</span>
      <span class="sep">·</span>
      <span>{{ t('apiEndpoints') }}</span>
      <span class="sep">·</span>
      <span>Fluid-Sinapis-Agent</span>
    </footer>
  </div>
</template>
