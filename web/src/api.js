import { t } from './i18n'

const BASE = import.meta.env.VITE_API_BASE ?? ''

async function getJSON(path, signal) {
  const res = await fetch(`${BASE}${path}`, {
    signal,
    headers: { Accept: 'application/json' },
  })
  if (!res.ok) {
    throw new Error(t('apiError', { path, status: res.status }))
  }
  return res.json()
}

export function fetchOverview(signal) {
  return getJSON('/api/overview', signal)
}

export function fetchCachedFiles(signal) {
  return getJSON('/api/cached-files', signal)
}
