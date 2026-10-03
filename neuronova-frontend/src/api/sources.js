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

  // Catalog routes deliberately use the same JWT-only transport as source
  // onboarding. A catalog snapshot, asset, or sync-run id is never itself an
  // authorization capability.
  getCatalog(sourceId, options) {
    return api.get(`/sources/${encodeURIComponent(sourceId)}/catalog`, jwtSourceOptions(options))
  },

  requestCatalogSync(sourceId, options) {
    return api.post(`/sources/${encodeURIComponent(sourceId)}/catalog-syncs`, {}, jwtSourceOptions(options))
  },

  getCatalogSyncRun(sourceId, runId, options) {
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/catalog-syncs/${encodeURIComponent(runId)}`,
      jwtSourceOptions(options),
    )
  },

  cancelCatalogSync(sourceId, runId, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/catalog-syncs/${encodeURIComponent(runId)}/cancel`,
      {},
      jwtSourceOptions(options),
    )
  },

  listCatalogAssets(sourceId, { snapshotId, page = 1, pageSize = 50, search = '' } = {}, options) {
    const query = new URLSearchParams()
    if (snapshotId) query.set('snapshot_id', snapshotId)
    query.set('page', String(page))
    query.set('page_size', String(pageSize))
    if (search) query.set('search', search)
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/catalog/assets?${query.toString()}`,
      jwtSourceOptions(options),
    )
  },

  getCatalogAsset(sourceId, assetId, { snapshotId } = {}, options) {
    const query = new URLSearchParams()
    if (snapshotId) query.set('snapshot_id', snapshotId)
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/catalog/assets/${encodeURIComponent(assetId)}?${query.toString()}`,
      jwtSourceOptions(options),
    )
  },

  getCatalogDiff(sourceId, { snapshotId } = {}, options) {
    const query = new URLSearchParams()
    if (snapshotId) query.set('snapshot_id', snapshotId)
    const suffix = query.size ? `?${query.toString()}` : ''
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/catalog/diff${suffix}`,
      jwtSourceOptions(options),
    )
  },

  // Database conversations are explicitly source-bound and JWT-only. A
  // session id is not itself an authorization capability; generic session
  // reads are still authenticated and resolved by the server.
  createChatSession(sourceId, body = { mode: 'AUTO' }, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/chat/sessions`,
      body,
      jwtSourceOptions(options),
    )
  },

  listChatSessions(sourceId, { limit = 50 } = {}, options) {
    const query = new URLSearchParams({ limit: String(limit) })
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/chat/sessions?${query.toString()}`,
      jwtSourceOptions(options),
    )
  },

  getChatSession(sessionId, options) {
    return api.get(`/chat/sessions/${encodeURIComponent(sessionId)}`, jwtSourceOptions(options))
  },

  getChatMessages(sessionId, { limit = 500 } = {}, options) {
    const query = new URLSearchParams({ limit: String(limit) })
    return api.get(
      `/chat/sessions/${encodeURIComponent(sessionId)}/messages?${query.toString()}`,
      jwtSourceOptions(options),
    )
  },

  streamChatMessage(sessionId, body, options) {
    return api.streamSSE(
      `/chat/sessions/${encodeURIComponent(sessionId)}/message`,
      body,
      jwtSourceOptions(options),
    )
  },

  cancelChatTurn(sessionId, turnId, options) {
    return api.post(
      `/chat/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/cancel`,
      {},
      {
        ...jwtSourceOptions(options),
        expectEmptyResponse: true,
      },
    )
  },
}
