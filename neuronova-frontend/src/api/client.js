import { createApi } from './request.js'

const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1'
const FALLBACK_API_KEY = import.meta.env.VITE_API_KEY || 'dev-local-key-change-me'

export const api = createApi({
  baseUrl: BASE_URL,
  fallbackApiKey: FALLBACK_API_KEY,
  storage: localStorage,
  onAuthExpired: () => { window.location.href = '/login' },
})
