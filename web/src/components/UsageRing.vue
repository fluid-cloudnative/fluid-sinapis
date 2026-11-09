<script setup>
import { computed } from 'vue'
import { t } from '../i18n'
import { formatPercent, usageLevel } from '../utils/format'

const props = defineProps({
  ratio: { type: Number, default: 0 },
  size: { type: Number, default: 136 },
  stroke: { type: Number, default: 11 },
  label: { type: String, default: '' },
})

const resolvedLabel = computed(() => props.label || t('usage'))

const center = computed(() => props.size / 2)
const radius = computed(() => props.size / 2 - props.stroke / 2 - 4)
const circumference = computed(() => 2 * Math.PI * radius.value)
const clamped = computed(() => Math.min(Math.max(Number(props.ratio) || 0, 0), 1))
const offset = computed(() => circumference.value * (1 - clamped.value))
const percent = computed(() => formatPercent(clamped.value))
const level = computed(() => usageLevel(clamped.value))
</script>

<template>
  <div class="ring" :class="`is-${level}`">
    <svg
      :width="size"
      :height="size"
      :viewBox="`0 0 ${size} ${size}`"
      role="img"
      :aria-label="`${resolvedLabel} ${percent}%`"
    >
      <circle
        class="ring-track"
        :cx="center"
        :cy="center"
        :r="radius"
        :stroke-width="stroke"
        fill="none"
      />
      <circle
        class="ring-value"
        :cx="center"
        :cy="center"
        :r="radius"
        :stroke-width="stroke"
        fill="none"
        stroke-linecap="round"
        :stroke-dasharray="circumference"
        :stroke-dashoffset="offset"
        :transform="`rotate(-90 ${center} ${center})`"
      />
    </svg>
    <div class="ring-center">
      <span class="ring-percent">
        {{ percent }}<i>%</i>
      </span>
      <span class="ring-label">{{ resolvedLabel }}</span>
    </div>
  </div>
</template>

<style scoped>
.ring {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.ring-track {
  stroke: var(--track);
}

.ring-value {
  stroke: var(--accent-ring);
  transition: stroke-dashoffset 520ms ease;
}

.ring-center {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
}

.ring-percent {
  font-size: 27px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text-strong);
  font-variant-numeric: tabular-nums;
}

.ring-percent i {
  font-size: 14px;
  font-weight: 500;
  font-style: normal;
  color: var(--text-muted);
  margin-left: 1px;
}

.ring-label {
  font-size: 11px;
  color: var(--text-muted);
}

.is-normal {
  --accent-ring: var(--ok);
  --track: #e6f4f1;
}

.is-warning {
  --accent-ring: var(--warn);
  --track: #fdf1dc;
}

.is-critical {
  --accent-ring: var(--danger);
  --track: #fbe9e7;
}

@media (prefers-reduced-motion: reduce) {
  .ring-value {
    transition: none;
  }
}
</style>
