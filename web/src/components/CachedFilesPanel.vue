<script setup>
import { computed } from 'vue'
import { t } from '../i18n'
import { formatBytes, formatDateTime } from '../utils/format'

const props = defineProps({
  data: { type: Object, default: null },
})

const files = computed(() => props.data?.files ?? [])
const total = computed(() => {
  const bytes = files.value.reduce((sum, file) => sum + (file.size_bytes || 0), 0)
  return formatBytes(bytes)
})
const summary = computed(() =>
  t('filesSummary', { n: files.value.length, size: `${total.value.value} ${total.value.unit}` }),
)
const isError = computed(() => props.data?.status === 'error')
</script>

<template>
  <section class="panel panel-fluid">
    <header class="panel-head">
      <div class="panel-title">
        <span class="panel-mark">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
               stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M4 5.5h16M4 12h16M4 18.5h16" />
            <path d="M9.5 5.5v13M15 5.5v13" opacity="0.45" />
          </svg>
        </span>
        <div class="panel-title-text">
          <h2>{{ t('cachedFiles') }}</h2>
          <p class="panel-sub">
            <template v-if="isError">{{ t('collectFailed') }}</template>
            <template v-else>
              {{ summary }}
              <template v-if="data?.scan_path"> · {{ t('scanPath', { path: data.scan_path }) }}</template>
            </template>
          </p>
        </div>
      </div>
      <span class="badge" :class="isError ? 'badge-muted' : 'badge-live'">
        <i class="badge-dot"></i>
        {{ isError ? t('badgeError') : t('badgeLive') }}
      </span>
    </header>

    <div v-if="isError" class="section-error">
      <span class="error-icon">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
             stroke-width="1.7" stroke-linecap="round" aria-hidden="true">
          <path d="M12 4.5 2.8 20h18.4L12 4.5Z" />
          <path d="M12 10v4.2M12 17.2h.01" />
        </svg>
      </span>
      <div class="error-body">
        <p class="error-title">{{ t('cachedFilesErrorTitle') }}</p>
        <p class="error-message">{{ data?.error }}</p>
        <ul class="error-hints">
          <li>{{ t('hintAlluxio') }}</li>
          <li>{{ t('hintAlluxioScan') }}</li>
        </ul>
      </div>
    </div>

    <div v-else-if="files.length" class="files-table-wrap">
      <table class="files-table">
        <thead>
          <tr>
            <th class="files-index">{{ t('thIndex') }}</th>
            <th>{{ t('thFile') }}</th>
            <th class="files-size">{{ t('thSize') }}</th>
            <th class="files-pct">{{ t('thCachedPct') }}</th>
            <th class="files-time">{{ t('thCachedAt') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(file, index) in files" :key="file.path">
            <td class="files-index">{{ index + 1 }}</td>
            <td class="files-name mono" :title="file.path">{{ file.name }}</td>
            <td class="files-size mono">
              {{ formatBytes(file.size_bytes).value }}
              <span class="files-unit">{{ formatBytes(file.size_bytes).unit }}</span>
            </td>
            <td class="files-pct mono">
              <span class="files-pct-value"
                    :class="file.cache_pct >= 100 ? 'is-full' : 'is-partial'">
                {{ file.cache_pct }}%
              </span>
            </td>
            <td class="files-time mono">{{ formatDateTime(file.cached_at_ms) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else class="files-empty">
      <p>{{ t('filesEmpty') }}</p>
      <p class="files-empty-hint">{{ t('filesEmptyHint') }}</p>
    </div>

    <p v-if="!isError && files.length" class="files-note">
      {{ t('filesNote') }}
    </p>
  </section>
</template>
