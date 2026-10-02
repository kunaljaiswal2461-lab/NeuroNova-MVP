import { api } from './client'

// PostgreSQL source endpoints are JWT-only on the backend. Keep that rule at
// the call site too so a missing browser session can never turn into a legacy
// global API-key request.
function jwtSourceOptions(options = {}) {
  return {
    ...options,
    requireJwt: true,
    preserveErrorBody: true,
  }
}

export function sourceErrorCode(error) {
  const backendCode = error?.response?.error?.code
  if (typeof backendCode === 'string') return backendCode
  return error?.code === 'authentication_required' ? error.code : null
}

export const sourceApi = {
  testPostgres(connection, options) {
    return api.post('/sources/postgresql/test', connection, jwtSourceOptions(options))
  },

  createSource(source, options) {
    return api.post('/sources', source, jwtSourceOptions(options))
  },

  listSources(options) {
    return api.get('/sources', jwtSourceOptions(options))
  },

  getSource(sourceId, options) {
    return api.get(`/sources/${encodeURIComponent(sourceId)}`, jwtSourceOptions(options))
  },

  deleteSource(sourceId, options) {
    return api.delete(`/sources/${encodeURIComponent(sourceId)}`, undefined, jwtSourceOptions(options))
  },
}
