<script setup>
import { computed } from 'vue'

const props = defineProps({
  name: { type: String, default: '' },
  size: { type: Number, default: 18 },
})

// Shared path sets: icons that look alike reuse one array instead of repeating it.
const CUBE_WITH_PINS = [
  'M7 7h10v10H7z',
  'M10 4v3M14 4v3M10 17v3M14 17v3M4 10h3M4 14h3M17 10h3M17 14h3',
]
const GAUGE = [
  'M4 18a8 8 0 1 1 16 0',
  'M12 18l4.5-4.5',
]

const ICONS = {
  k8s_nodes: [
    'M3.5 4.5h17v6h-17z',
    'M3.5 13.5h17v6h-17z',
    'M7 7.5h.01M7 16.5h.01',
  ],
  k8s_node_allocatable_memory_bytes: CUBE_WITH_PINS,
  k8s_free_memory_bytes: CUBE_WITH_PINS,
  k8s_ready_pods: [
    'M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z',
    'M4 7.5L12 12l8-4.5M12 12v9',
  ],
  fluid_cache_capacity_bytes: [
    'M20 6c0 1.7-3.6 3-8 3S4 7.7 4 6s3.6-3 8-3 8 1.3 8 3z',
    'M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6',
    'M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6',
  ],
  fluid_cache_used_bytes: [
    'M21 12a9 9 0 1 1-9-9v9h9z',
  ],
  fluid_cache_capacity_per_worker_bytes: [
    'M12 3l8 4.5-8 4.5-8-4.5L12 3z',
    'M4 12l8 4.5 8-4.5',
    'M4 16.5L12 21l8-4.5',
  ],
  fluid_workers: [
    'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  ],
  fluid_cache_used_ratio: GAUGE,
  derived_memory_allocated_ratio: GAUGE,
}

const paths = computed(() => ICONS[props.name] ?? ['M12 8v8M8 12h8'])
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path v-for="(d, i) in paths" :key="i" :d="d" />
  </svg>
</template>
