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

  retestSavedSource(sourceId, options) {
    return api.post(`/sources/${encodeURIComponent(sourceId)}/retest`, {}, jwtSourceOptions(options))
  },

  replaceSavedSourceCredentials(sourceId, body, options) {
    return api.put(`/sources/${encodeURIComponent(sourceId)}/credentials`, body, jwtSourceOptions(options))
  },

  updateSavedSourceScope(sourceId, body, options) {
    return api.put(`/sources/${encodeURIComponent(sourceId)}/scope`, body, jwtSourceOptions(options))
  },

  configureCatalogRefresh(sourceId, body, options) {
    return api.put(`/sources/${encodeURIComponent(sourceId)}/catalog-refresh`, body, jwtSourceOptions(options))
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

  // Database profiling is intentionally source-scoped and JWT-only. The
  // browser submits catalog UUIDs only; it never submits a relation name,
  // SQL, sample size, or profiling operation.
  configureProfiling(sourceId, body, options) {
    return api.put(
      `/sources/${encodeURIComponent(sourceId)}/profiling`,
      body,
      jwtSourceOptions(options),
    )
  },

  // AI and privacy settings (SPEC-09 W3). The sample-values endpoints return names and counts only, never a value.
  getAiSettings(sourceId, options) {
    return api.get(`/sources/${encodeURIComponent(sourceId)}/ai-settings`, jwtSourceOptions(options))
  },

  updateAiSettings(sourceId, body, options) {
    return api.put(`/sources/${encodeURIComponent(sourceId)}/ai-settings`, body, jwtSourceOptions(options))
  },

  getSampleValues(sourceId, options) {
    return api.get(`/sources/${encodeURIComponent(sourceId)}/sample-values`, jwtSourceOptions(options))
  },

  updateSampleValues(sourceId, body, options) {
    return api.put(`/sources/${encodeURIComponent(sourceId)}/sample-values`, body, jwtSourceOptions(options))
  },

  getColumnPrivacy(sourceId, options) {
    return api.get(`/sources/${encodeURIComponent(sourceId)}/column-privacy`, jwtSourceOptions(options))
  },

  updateColumnPrivacy(sourceId, body, options) {
    return api.put(`/sources/${encodeURIComponent(sourceId)}/column-privacy`, body, jwtSourceOptions(options))
  },

  requestProfileRun(sourceId, body, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/profile-runs`,
      body,
      jwtSourceOptions(options),
    )
  },

  listProfileRuns(sourceId, { page = 1, pageSize = 20 } = {}, options) {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/profile-runs?${query.toString()}`,
      jwtSourceOptions(options),
    )
  },

  getProfileRun(sourceId, runId, options) {
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/profile-runs/${encodeURIComponent(runId)}`,
      jwtSourceOptions(options),
    )
  },

  cancelProfileRun(sourceId, runId, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/profile-runs/${encodeURIComponent(runId)}/cancel`,
      {},
      jwtSourceOptions(options),
    )
  },

  getProfileObservations(sourceId, runId, { page = 1, pageSize = 100 } = {}, options) {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/profile-runs/${encodeURIComponent(runId)}/observations?${query.toString()}`,
      jwtSourceOptions(options),
    )
  },

  getProfileFindings(sourceId, runId, { page = 1, pageSize = 100 } = {}, options) {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/profile-runs/${encodeURIComponent(runId)}/findings?${query.toString()}`,
      jwtSourceOptions(options),
    )
  },

  // Confirmed metric definitions remain source-owned resources. IDs in these
  // paths are opaque references, never authorization capabilities; every
  // request still carries the current user's JWT.
  listMetricDefinitions(sourceId, options) {
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/metrics`,
      jwtSourceOptions(options),
    )
  },

  createMetricDefinition(sourceId, body, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/metrics`,
      body,
      jwtSourceOptions(options),
    )
  },

  getMetricDefinition(sourceId, metricId, options) {
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/metrics/${encodeURIComponent(metricId)}`,
      jwtSourceOptions(options),
    )
  },

  createMetricDefinitionVersion(sourceId, metricId, body, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/metrics/${encodeURIComponent(metricId)}/versions`,
      body,
      jwtSourceOptions(options),
    )
  },

  listMetricDefinitionVersions(sourceId, metricId, options) {
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/metrics/${encodeURIComponent(metricId)}/versions`,
      jwtSourceOptions(options),
    )
  },

  retireMetricDefinition(sourceId, metricId, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/metrics/${encodeURIComponent(metricId)}/retire`,
      {},
      jwtSourceOptions(options),
    )
  },

  // Approved relationships are source-owned catalog contracts, not a way to
  // submit an arbitrary join. The backend derives the mapping from an
  // in-scope declared foreign key and still applies the user's JWT on every
  // request.
  listRelationshipDefinitions(sourceId, options) {
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/relationships`,
      jwtSourceOptions(options),
    )
  },

  listEligibleDeclaredRelationships(sourceId, options) {
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/relationships/eligible`,
      jwtSourceOptions(options),
    )
  },

  createRelationshipDefinition(sourceId, body, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/relationships`,
      body,
      jwtSourceOptions(options),
    )
  },

  getRelationshipDefinition(sourceId, relationshipId, options) {
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/relationships/${encodeURIComponent(relationshipId)}`,
      jwtSourceOptions(options),
    )
  },

  createRelationshipDefinitionVersion(sourceId, relationshipId, body, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/relationships/${encodeURIComponent(relationshipId)}/versions`,
      body,
      jwtSourceOptions(options),
    )
  },

  listRelationshipDefinitionVersions(sourceId, relationshipId, options) {
    return api.get(
      `/sources/${encodeURIComponent(sourceId)}/relationships/${encodeURIComponent(relationshipId)}/versions`,
      jwtSourceOptions(options),
    )
  },

  retireRelationshipDefinition(sourceId, relationshipId, options) {
    return api.post(
      `/sources/${encodeURIComponent(sourceId)}/relationships/${encodeURIComponent(relationshipId)}/retire`,
      {},
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

  async *streamChatMessage(sessionId, body, options) {
    // Refresh a near-expiry access token first: a stream is never refreshed and replayed after a 401 (SPEC-10 W1.5).
    await api.ensureFreshToken?.()
    yield* api.streamSSE(
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
