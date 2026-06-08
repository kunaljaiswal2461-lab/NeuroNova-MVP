import { useState, useEffect } from 'react'
import Layout from '../components/Layout'
import { api } from '../api/client'
import { useDataset } from '../context/DatasetContext'

const SEMANTIC_TAG_MAP = {
  IDENTIFIER: 'type-identifier',
  FINANCIAL:  'type-financial',
  TEMPORAL:   'type-temporal',
  GEOGRAPHIC: 'type-geographic',
  EMAIL:      'type-categorical',
  CATEGORICAL:'type-categorical',
  BOOLEAN:    'type-boolean',
  NUMERIC:    'type-numeric',
  TEXT:       'type-categorical',
  URL:        'type-categorical',
  PHONE:      'type-categorical',
  UNKNOWN:    '',
}

function gradeFromNullPct(pct) {
  if (pct < 5)  return { grade: 'A', cls: 'grade-a' }
  if (pct < 15) return { grade: 'B', cls: 'grade-b' }
  if (pct < 30) return { grade: 'C', cls: 'grade-c' }
  if (pct < 50) return { grade: 'D', cls: 'grade-d' }
  return { grade: 'F', cls: 'grade-f' }
}

function findingToFlag(f) {
  const severityStyle = {
    HIGH:   { bg: '#FFF1F2', border: '#FECDD3', titleColor: '#9F1239', icon: '⚠' },
    MEDIUM: { bg: '#FFFBEB', border: '#FDE68A', titleColor: '#92400E', icon: '⚡' },
    LOW:    { bg: '#F0F9FF', border: '#BAE6FD', titleColor: '#075985', icon: 'ℹ' },
  }[f.severity] ?? { bg: '#F9FAFB', border: '#E5E7EB', titleColor: '#374151', icon: '·' }

  return {
    ...severityStyle,
    title: f.title ?? f.type,
    detail: f.column ? `Column: ${f.column}` : (f.body ?? ''),
  }
}

export default function Explorer() {
  const { activeDatasetId, activeDataset, selectDataset } = useDataset()
  const [searchQuery, setSearchQuery] = useState('')
  const [mounted, setMounted] = useState(false)
  const [loading, setLoading] = useState(false)
  const [profile, setProfile] = useState(null)
  const [anomalyFlags, setAnomalyFlags] = useState([])
  const [skippedVizCols, setSkippedVizCols] = useState(new Set())
  const [error, setError] = useState(null)

  useEffect(() => { setTimeout(() => setMounted(true), 100) }, [])

  useEffect(() => {
    if (!activeDatasetId) return
    setLoading(true)
    setError(null)
    Promise.all([
      api.get(`/datasets/${activeDatasetId}/profile`),
      api.get(`/datasets/${activeDatasetId}/findings?severity=HIGH`),
      api.get(`/datasets/${activeDatasetId}/viz`).catch(() => ({ skipped_columns: [] })),
    ])
      .then(([prof, findingsResp, vizResp]) => {
        setProfile(prof)
        setAnomalyFlags((findingsResp.findings ?? []).slice(0, 6).map(findingToFlag))
        setSkippedVizCols(new Set((vizResp.skipped_columns ?? []).map(s => s.column_name)))
        // push health into context so Sidebar can show it
        if (activeDataset) {
          selectDataset({ ...activeDataset, _health: prof.health })
        }
      })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }, [activeDatasetId])

  // Build merged column rows
  const schemaColumns = (() => {
    if (!profile) return []
    const semanticMap = Object.fromEntries((profile.semantic?.columns ?? []).map(s => [s.name, s.semantic_type]))
    const qualityMap  = Object.fromEntries((profile.quality?.columns  ?? []).map(q => [q.name, q]))
    const statsMap    = Object.fromEntries((profile.stats?.columns    ?? []).map(s => [s.name, s]))

    return (profile.schema?.columns ?? []).map(col => {
      const qual = qualityMap[col.name]
      const stat = statsMap[col.name]
      const nullPct = qual?.null_pct ?? 0
      const cardinality = stat?.categorical?.cardinality ?? stat?.numeric?.count ?? '—'
      const { grade, cls } = gradeFromNullPct(nullPct)
      return {
        name: col.name,
        dtype: col.dtype,
        semantic: semanticMap[col.name] ?? 'UNKNOWN',
        nullPct: `${nullPct.toFixed(1)}%`,
        nullPctRaw: nullPct,
        cardinality: typeof cardinality === 'number' ? cardinality.toLocaleString() : cardinality,
        grade,
        gradeClass: cls,
      }
    })
  })()

  const filteredCols = schemaColumns.filter(col =>
    col.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    col.semantic.toLowerCase().includes(searchQuery.toLowerCase())
  )

  const health = profile?.health ?? null
  const healthComponents = health ? Object.entries(health.components ?? {}) : []

  const stats = profile ? [
    { label: 'Total Rows',   val: profile.schema?.row_count?.toLocaleString() ?? '—' },
    { label: 'Columns',      val: profile.schema?.col_count ?? '—' },
    { label: 'Null Rate',    val: `${(schemaColumns.reduce((s, c) => s + c.nullPctRaw, 0) / Math.max(schemaColumns.length, 1)).toFixed(1)}%` },
    { label: 'Duplicates',   val: `${(profile.quality?.duplicate_row_pct ?? 0).toFixed(1)}%` },
    { label: 'Health Score', val: health ? `${Math.round(health.score)} / 100` : '—' },
    { label: 'Grade',        val: health?.grade ?? '—' },
  ] : []

  const noDataset = !activeDatasetId
  const subtitle = activeDataset?.original_name ?? 'No dataset selected'

  return (
    <Layout
      title="Dataset Explorer"
      subtitle={subtitle}
      actions={
        <button className="btn btn-ghost-neutral" style={{ gap: 6, fontSize: 13 }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="1 4 1 10 7 10" /><path d="M3.51 15a9 9 0 1 0 .49-3.51" />
          </svg>
          Re-profile
        </button>
      }
    >
      {noDataset ? (
        <div className="card card-pad anim-fade-up" style={{ textAlign: 'center', padding: '48px 24px' }}>
          <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 18, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 8 }}>No Dataset Selected</h2>
          <p style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'var(--color-text-secondary)', marginBottom: 20 }}>
            Upload a dataset and select it from the Upload Center to explore its schema and profile.
          </p>
          <a href="/upload" className="btn btn-primary" style={{ display: 'inline-flex', justifyContent: 'center' }}>Go to Upload →</a>
        </div>
      ) : error ? (
        <div style={{ background: '#FFF1F2', border: '1px solid #FECDD3', borderRadius: 8, padding: '16px 20px', fontFamily: 'var(--font-body)', fontSize: 13, color: '#9F1239' }}>
          {error}
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: 20, alignItems: 'start' }}>

          {/* ── Left: Schema Table ── */}
          <div className="card anim-fade-up">
            <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--color-border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)' }}>Schema Overview</h3>
              <div style={{ position: 'relative' }}>
                <svg style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)' }} width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--color-text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
                </svg>
                <input
                  className="input"
                  style={{ paddingLeft: 28, width: 200, fontSize: 12, padding: '7px 10px 7px 28px' }}
                  placeholder="Search columns..."
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                />
              </div>
            </div>

            <div style={{ overflowX: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Column</th>
                    <th>Type</th>
                    <th>Semantic</th>
                    <th>Null%</th>
                    <th>Cardinality</th>
                    <th>Viz</th>
                    <th>Grade</th>
                  </tr>
                </thead>
                <tbody>
                  {loading ? (
                    <tr>
                      <td colSpan={7} style={{ textAlign: 'center', padding: '40px 16px', fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>
                        Loading profile…
                      </td>
                    </tr>
                  ) : filteredCols.length === 0 ? (
                    <tr>
                      <td colSpan={7} style={{ textAlign: 'center', padding: '40px 16px' }}>
                        <div style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)', marginBottom: 6 }}>
                          {searchQuery ? 'No columns match your search' : 'No schema data yet'}
                        </div>
                        {!searchQuery && (
                          <div style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', opacity: 0.7 }}>Upload and profile a dataset to see schema details</div>
                        )}
                      </td>
                    </tr>
                  ) : (
                    filteredCols.map((col, i) => (
                      <tr key={i} style={{ cursor: 'pointer' }}>
                        <td>
                          <span style={{ fontFamily: 'var(--font-data)', fontSize: 13, color: 'var(--color-text-primary)', fontWeight: 500 }}>{col.name}</span>
                        </td>
                        <td>
                          <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-primary-indigo)', background: '#EDEAFF', padding: '2px 6px', borderRadius: 4 }}>
                            {col.dtype}
                          </span>
                        </td>
                        <td>
                          <span className={`type-tag ${SEMANTIC_TAG_MAP[col.semantic] || ''}`}>{col.semantic}</span>
                        </td>
                        <td>
                          <span style={{
                            fontFamily: 'var(--font-data)', fontSize: 12,
                            color: col.nullPctRaw > 10 ? 'var(--color-danger)' : col.nullPctRaw > 5 ? 'var(--color-warning)' : 'var(--color-text-secondary)',
                            fontWeight: col.nullPctRaw > 10 ? 600 : 400,
                          }}>
                            {col.nullPct}
                          </span>
                        </td>
                        <td><span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-secondary)' }}>{col.cardinality}</span></td>
                        <td>
                          {skippedVizCols.has(col.name) ? (
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }} title="Column was skipped in the viz layer">
                              ⊘ Skipped
                            </span>
                          ) : (
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: '#0F766E' }}>
                              ✓ Rendered
                            </span>
                          )}
                        </td>
                        <td><span className={`grade-badge ${col.gradeClass}`}>{col.grade}</span></td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            <div style={{ padding: '10px 20px', borderTop: '1px solid var(--color-border-light)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)' }}>
                {filteredCols.length} of {schemaColumns.length} columns
              </span>
              <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>
                {profile?.schema?.row_count?.toLocaleString() ?? '—'} rows
              </span>
            </div>
          </div>

          {/* ── Right Panel ── */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>

            {/* Dataset Health */}
            <div className="card card-pad anim-fade-up" style={{ animationDelay: '60ms' }}>
              <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 15, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 16 }}>Dataset Health</h3>

              {!health || loading ? (
                <div style={{ textAlign: 'center', padding: '24px 0' }}>
                  <div style={{ fontFamily: 'var(--font-display)', fontSize: 40, fontWeight: 700, color: 'var(--color-border)', lineHeight: 1 }}>—</div>
                  <div style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', marginTop: 8 }}>{loading ? 'Loading…' : 'No health data yet'}</div>
                </div>
              ) : (
                <>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 20 }}>
                    <span style={{ fontFamily: 'var(--font-display)', fontSize: 48, fontWeight: 700, color: 'var(--color-text-primary)', lineHeight: 1 }}>{Math.round(health.score)}</span>
                    <div>
                      <div style={{ fontFamily: 'var(--font-heading)', fontSize: 14, color: 'var(--color-text-secondary)' }}>/ 100 · {health.grade}</div>
                    </div>
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    {healthComponents.map(([key, val], i) => (
                      <div key={key}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                          <span style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)', textTransform: 'capitalize' }}>{key}</span>
                          <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>{Math.round(val)}%</span>
                        </div>
                        <div className="health-bar-track">
                          <div className="health-bar-fill" style={{
                            width: mounted ? `${val}%` : '0%',
                            background: val >= 80 ? '#34D399' : val >= 60 ? '#FBBF24' : '#F87171',
                            transitionDelay: `${i * 80}ms`,
                          }} />
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>

            {/* Anomaly Flags */}
            <div className="card anim-fade-up" style={{ animationDelay: '120ms' }}>
              <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--color-border)' }}>
                <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 14, fontWeight: 600, color: 'var(--color-text-primary)' }}>High-Severity Flags</h3>
              </div>
              <div style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 8 }}>
                {loading ? (
                  <div style={{ textAlign: 'center', padding: '16px 0', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)' }}>Loading…</div>
                ) : anomalyFlags.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: '16px 0' }}>
                    <div style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)' }}>No high-severity flags</div>
                  </div>
                ) : (
                  anomalyFlags.map((flag, i) => (
                    <div key={i} style={{ background: flag.bg, border: `1px solid ${flag.border}`, borderRadius: 8, padding: '10px 12px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                        <span style={{ fontSize: 13 }}>{flag.icon}</span>
                        <span style={{ fontFamily: 'var(--font-heading)', fontSize: 13, fontWeight: 600, color: flag.titleColor }}>{flag.title}</span>
                      </div>
                      <p style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-secondary)', paddingLeft: 21 }}>{flag.detail}</p>
                    </div>
                  ))
                )}
              </div>
            </div>

            {/* Quick Stats */}
            {stats.length > 0 && (
              <div className="card card-pad anim-fade-up" style={{ animationDelay: '180ms' }}>
                <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 14, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 12 }}>Dataset Stats</h3>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                  {stats.map((stat, i) => (
                    <div key={i} style={{ background: 'var(--color-base)', borderRadius: 6, padding: '8px 10px' }}>
                      <div style={{ fontFamily: 'var(--font-body)', fontSize: 10, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 3 }}>{stat.label}</div>
                      <div style={{ fontFamily: 'var(--font-display)', fontSize: 18, fontWeight: 700, color: 'var(--color-text-primary)' }}>{stat.val}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </Layout>
  )
}
