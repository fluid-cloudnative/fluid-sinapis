<script setup>
import { computed } from 'vue'
import MetricIcon from './MetricIcon.vue'
import { metricHelp, metricLabel, t } from '../i18n'
import { formatMetric } from '../utils/format'

const props = defineProps({
  metric: { type: Object, required: true },
})

const view = computed(() => formatMetric(props.metric))
const label = computed(() => metricLabel(props.metric.name, props.metric.label))
const help = computed(() => metricHelp(props.metric.name, props.metric.help))
</script>

<template>
  <article class="metric-card">
    <header class="metric-head">
      <span class="metric-icon">
        <MetricIcon :name="metric.name" />
      </span>
      <span class="metric-label">{{ label }}</span>
      <span v-if="metric.derived" class="metric-tag">{{ t('derivedTag') }}</span>
    </header>
    <div class="metric-value">
      <span class="metric-number">{{ view.value }}</span>
      <span v-if="view.unit" class="metric-unit">{{ view.unit }}</span>
    </div>
    <p class="metric-help" :title="help">{{ help }}</p>
  </article>
</template>
