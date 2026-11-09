<script setup>
import { computed } from 'vue'
import MetricCard from './MetricCard.vue'
import MetricIcon from './MetricIcon.vue'
import UsageRing from './UsageRing.vue'
import { metricHelp, metricLabel, sectionTitle, t } from '../i18n'
import { formatCount, formatTime } from '../utils/format'

const props = defineProps({
  section: { type: Object, required: true },
})

const accent = computed(() => (props.section.key === 'fluid' ? 'fluid' : 'k8s'))
const featured = computed(
  () => props.section.metrics.find((metric) => metric.unit === 'ratio') ?? null,
)
const cards = computed(() =>
  props.section.metrics.filter((metric) => metric !== featured.value),
)
const caption = computed(() =>
  props.section.key === 'fluid' ? t('fluidCaption') : t('k8sCaption'),
)
const panelIcon = computed(() =>
  props.section.key === 'fluid' ? 'fluid_cache_capacity_bytes' : 'k8s_nodes',
)
const title = computed(() => sectionTitle(props.section.key, props.section.title))
const featuredLabel = computed(() =>
  featured.value ? metricLabel(featured.value.name, featured.value.label) : '',
)
const featuredHelp = computed(() =>
  featured.value ? metricHelp(featured.value.name, featured.value.help) : '',
)

function labelsText(labels) {
  const entries = Object.entries(labels ?? {})
  return entries.length ? entries.map(([key, value]) => `${key}=${value}`).join(' · ') : '—'
}

function rawValue(value) {
  if (!Number.isFinite(value)) return '—'
  if (Number.isInteger(value)) return formatCount(value)
  return value.toFixed(3)
}
</script>

<template>
  <section class="panel" :class="`panel-${accent}`">
    <header class="panel-head">
      <div class="panel-title">
        <span class="panel-mark">
          <MetricIcon :name="panelIcon" :size="18" />
        </span>
        <div class="panel-title-text">
          <h2>{{ title }}</h2>
          <p class="panel-sub">
            <template v-if="section.status === 'ok'">
              {{ t('metricCount', { n: section.metrics.length }) }}
              <template v-if="section.collected_at_ms">
                · {{ t('collectedAt', { time: formatTime(section.collected_at_ms) }) }}
              </template>
            </template>
            <template v-else>{{ t('collectFailed') }}</template>
          </p>
        </div>
      </div>
      <span class="badge" :class="section.status === 'error' ? 'badge-muted' : 'badge-live'">
        <i class="badge-dot"></i>
        {{ section.status === 'error' ? t('badgeError') : t('badgeLive') }}
      </span>
    </header>

    <div v-if="section.status === 'error'" class="section-error">
      <span class="error-icon">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
             stroke-width="1.7" stroke-linecap="round" aria-hidden="true">
          <path d="M12 4.5 2.8 20h18.4L12 4.5Z" />
          <path d="M12 10v4.2M12 17.2h.01" />
        </svg>
      </span>
      <div class="error-body">
        <p class="error-title">{{ t('collectFailedTitle') }}</p>
        <p class="error-message">{{ section.error }}</p>
        <ul class="error-hints">
          <li>{{ t('hintKubeBefore') }} <code>KUBE_CONTEXT</code> {{ t('hintKubeAfter') }}</li>
          <li>{{ t('hintRbac') }}</li>
        </ul>
      </div>
    </div>

    <div v-else class="panel-body">
      <div v-if="featured" class="feature-card">
        <div class="feature-head">
          <span class="feature-label">{{ featuredLabel }}</span>
          <span class="feature-caption">{{ caption }}</span>
        </div>
        <UsageRing :ratio="featured.value" :size="148" :label="t('usage')" />
        <p class="feature-help">{{ featuredHelp }}</p>
      </div>
      <div class="card-grid">
        <MetricCard v-for="metric in cards" :key="metric.name" :metric="metric" />
      </div>
    </div>

    <details v-if="section.status === 'ok' && section.metrics.length" class="raw">
      <summary>
        {{ t('rawMetrics') }}
        <span class="raw-count">{{ section.metrics.length }}</span>
      </summary>
      <div class="raw-table-wrap">
        <table class="raw-table">
          <thead>
            <tr>
              <th>{{ t('thMetric') }}</th>
              <th>{{ t('thValue') }}</th>
              <th>{{ t('thLabels') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="metric in section.metrics" :key="metric.name">
              <td class="mono">{{ metric.name }}</td>
              <td class="mono raw-value">{{ rawValue(metric.value) }}</td>
              <td class="mono raw-labels" :title="labelsText(metric.labels)">
                {{ labelsText(metric.labels) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
  </section>
</template>
