import { localeTag } from '../i18n'

const BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']

/** Bytes -> { value, unit }, base 1024, one decimal (no decimal for B). */
export function formatBytes(bytes) {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n <= 0) return { value: '0', unit: 'B' }
  let index = 0
  let value = n
  while (value >= 1024 && index < BYTE_UNITS.length - 1) {
    value /= 1024
    index += 1
  }
  return {
    value: index === 0 ? String(Math.round(value)) : value.toFixed(1),
    unit: BYTE_UNITS[index],
  }
}

/** Integer with thousands separators. */
export function formatCount(count) {
  const n = Number(count)
  if (!Number.isFinite(n)) return '—'
  return Math.round(n).toLocaleString(localeTag())
}

/** 0..1 -> percentage string without the % sign. */
export function formatPercent(ratio, digits = 1) {
  const n = Number(ratio)
  if (!Number.isFinite(n)) return '—'
  return (Math.min(Math.max(n, 0), 1) * 100).toFixed(digits)
}

/** Return the display value and unit suffix for a metric, used directly by cards. */
export function formatMetric(metric) {
  switch (metric.unit) {
    case 'bytes': {
      const { value, unit } = formatBytes(metric.value)
      return { value, unit }
    }
    case 'ratio':
      return { value: formatPercent(metric.value), unit: '%' }
    default: {
      // Chinese keeps the counter suffix; English shows the bare number
      return { value: formatCount(metric.value), unit: localeTag() === 'zh-CN' ? '个' : '' }
    }
  }
}

export function formatTime(ms) {
  if (!ms) return '—'
  return new Date(ms).toLocaleTimeString(localeTag(), { hour12: false })
}

/** Full date + time; used when the day itself matters (a file can stay cached for days). */
export function formatDateTime(ms) {
  if (!ms) return '—'
  return new Date(ms).toLocaleString(localeTag(), { hour12: false })
}

/** Usage banding: picks the colour for rings and progress bars. */
export function usageLevel(ratio) {
  const n = Number(ratio) || 0
  if (n >= 0.85) return 'critical'
  if (n >= 0.7) return 'warning'
  return 'normal'
}
