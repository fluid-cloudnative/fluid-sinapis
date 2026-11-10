import { t } from './i18n'

const BASE = import.meta.env.VITE_API_BASE ?? ''
const KEY_STORAGE = 'sinapis.api-key'

export class AuthError extends Error {
  constructor(message) {
    super(message)
    this.name = 'AuthError'
  }
}

export function readStoredApiKey() {
  try {
    return localStorage.getItem(KEY_STORAGE) ?? ''
  } catch {
    return ''
  }
}

export function storeApiKey(key) {
  try {
    localStorage.setItem(KEY_STORAGE, key)
  } catch {
    /* storage unavailable: the key just won't persist across reloads */
  }
}

async function getJSON(path, signal) {
  const key = readStoredApiKey()
  const headers = { Accept: 'application/json' }
  if (key) headers.Authorization = `Bearer ${key}`
  const res = await fetch(`${BASE}${path}`, {
    signal,
    headers,
  })
  if (res.status === 401) {
    throw new AuthError(t('apiKeyRequired'))
  }
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
