import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import Layout from '../components/Layout'
import { api } from '../api/client'
import { useDataset } from '../context/DatasetContext'

export default function Insights() {
  const { activeDatasetId, activeDataset } = useDataset()
  const [loading, setLoading] = useState(false)
  const [insights, setInsights] = useState(null)
  const [highFindings, setHighFindings] = useState([])
  const [mediumFindings, setMediumFindings] = useState([])
  const [error, setError] = useState(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (!activeDatasetId) return
    setLoading(true)
    setError(null)
    Promise.all([
      api.get(`/datasets/${activeDatasetId}/insights`),
      api.get(`/datasets/${activeDatasetId}/findings?severity=HIGH`),
      api.get(`/datasets/${activeDatasetId}/findings?severity=MEDIUM`),
    ])
      .then(([ins, highResp, medResp]) => {
        setInsights(ins)
        setHighFindings(highResp.findings ?? [])
        setMediumFindings(medResp.findings ?? [])
      })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }, [activeDatasetId])

  const handleSuggestedQuestion = (q) => {
    navigate('/chat', { state: { prefill: q } })
  }

  const exec = insights?.executive_summary ?? null
  const explanations = insights?.finding_explanations ?? []
  const suggestedQuestions = insights?.suggested_questions ?? []
  const tags = exec ? [...(exec.key_strengths ?? []).map(s => `✓ ${s}`).slice(0, 3), ...(exec.key_concerns ?? []).map(s => `⚠ ${s}`).slice(0, 2)] : []

  const noDataset = !activeDatasetId
  const noData = !loading && !insights && !error

  if (noDataset) {
    return (
      <Layout className="insights-page" title="AI Insights" subtitle="LLM-powered analysis grounded in your dataset findings">
        <div className="card card-pad anim-fade-up" style={{ textAlign: 'center', padding: '48px 24px', maxWidth: 820 }}>
          <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 18, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 8 }}>No Dataset Selected</h2>
          <p style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'var(--color-text-secondary)', maxWidth: 380, margin: '0 auto 20px', lineHeight: 1.6 }}>
            Upload and profile a dataset to generate AI-powered findings, anomaly detection, and follow-up queries.
          </p>
          <a href="/upload" className="btn btn-primary" style={{ display: 'inline-flex', justifyContent: 'center' }}>Upload a Dataset →</a>
        </div>
      </Layout>
    )
  }

  return (
    <Layout
      className="insights-page"
      title="AI Insights"
      subtitle={activeDataset?.original_name ?? 'LLM-powered analysis grounded in your dataset findings'}
      actions={
        <button className="btn btn-ghost-neutral" style={{ gap: 6, fontSize: 13 }} onClick={() => { setInsights(null); setHighFindings([]); setMediumFindings([]); if (activeDatasetId) { setLoading(true); Promise.all([api.get(`/datasets/${activeDatasetId}/insights`), api.get(`/datasets/${activeDatasetId}/findings?severity=HIGH`), api.get(`/datasets/${activeDatasetId}/findings?severity=MEDIUM`)]).then(([ins, h, m]) => { setInsights(ins); setHighFindings(h.findings ?? []); setMediumFindings(m.findings ?? []) }).catch(err => setError(err.message)).finally(() => setLoading(false)) } }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="1 4 1 10 7 10" /><path d="M3.51 15a9 9 0 1 0 .49-3.51" />
          </svg>
          Refresh
        </button>
      }
    >
      <div className="insights-workspace">

        {error && (
          <div className="insights-error">
            {error}
          </div>
        )}

        {/* Tags */}
        {tags.length > 0 && (
          <div className="insights-tags">
            {tags.map((tag, i) => (
              <span key={i} className="tag">{tag}</span>
            ))}
          </div>
        )}

        {/* Executive Summary */}
        {loading ? (
          <div className="card anim-fade-up insights-loading-state">
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>Loading AI insights…</p>
          </div>
        ) : !exec ? (
          <div className="card card-pad anim-fade-up insights-empty-state">
            <div style={{ width: 52, height: 52, borderRadius: '50%', background: '#F0EDF9', border: '1px solid #DDD9F5', display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 16px' }}>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--color-primary-indigo)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
              </svg>
            </div>
            <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 18, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 8 }}>No Insights Yet</h2>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'var(--color-text-secondary)', maxWidth: 380, margin: '0 auto 20px', lineHeight: 1.6 }}>
              {insights?.degraded ? `Insights degraded: ${insights.degraded_reason ?? 'LLM unavailable'}` : 'Insights will appear after the profiling pipeline completes.'}
            </p>
          </div>
        ) : (
          <div className="card anim-fade-up insights-executive-card">
            <div className="insights-executive-heading">
              <p className="text-label-caps" style={{ color: 'var(--color-primary-indigo)' }}>Executive Summary</p>
              <span className="insights-model-badge">
                ● {insights?.model_used ?? 'gpt-4o'}
              </span>
            </div>
            <h2 className="insights-executive-headline">
              {exec.headline}
            </h2>
            <p className="insights-executive-overview">
              {exec.overview}
            </p>
            {exec.recommended_next_steps?.length > 0 && (
              <div className="insights-next-steps">
                <p className="insights-next-steps-label">Next Steps</p>
                {exec.recommended_next_steps.map((step, i) => (
                  <div key={i} className="insights-next-step">
                    <span className="insights-next-step-index">{i + 1}.</span>
                    <span className="insights-next-step-text">{step}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* High-Severity Findings */}
        {highFindings.length > 0 && (
          <div className="insights-section">
            <h3 className="insights-section-title">
              High-Severity Findings
            </h3>
            <div className="insights-findings-list">
              {highFindings.map(finding => {
                const explanation = explanations.find(e => String(e.finding_id) === String(finding.id))
                const findingText = explanation?.plain_english ?? finding.body ?? ''
                return (
                  <div key={finding.id} className="anim-fade-up insight-finding insight-finding-high">
                    <div className="insight-finding-header">
                      <div className="insight-finding-title-wrap">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-danger)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                          <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
                        </svg>
                        <span className="insight-finding-title">
                          {explanation?.title ?? finding.title ?? finding.type}
                        </span>
                      </div>
                      {finding.column && (
                        <span className="insight-finding-column">
                          Col: {finding.column}
                        </span>
                      )}
                    </div>

                    {findingText && <p className="insight-finding-body">{findingText}</p>}

                    {/* Evidence chips */}
                    {finding.evidence && Object.keys(finding.evidence).length > 0 && (
                      <div className="insight-evidence">
                        {Object.entries(finding.evidence).slice(0, 6).map(([k, v]) => (
                          <div key={k} className="insight-evidence-chip">
                            <span className="insight-evidence-key">{k}: </span>
                            <span className="insight-evidence-value">{String(v)}</span>
                          </div>
                        ))}
                        <div className="insight-evidence-chip">
                          <span className="insight-evidence-key">confidence: </span>
                          <span className="insight-evidence-value">{((finding.confidence ?? 0) * 100).toFixed(0)}%</span>
                        </div>
                      </div>
                    )}

                    {/* Recommended Action */}
                    {explanation?.suggested_action && (
                      <div className="insight-recommended-action">
                        <div className="insight-recommended-action-heading">
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--color-info)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                          </svg>
                          <span className="insight-recommended-action-label">Recommended Action</span>
                        </div>
                        <p className="insight-recommended-action-text">
                          {explanation.suggested_action}
                        </p>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Medium Findings */}
        {mediumFindings.length > 0 && (
          <div className="insights-section">
            <h3 className="insights-section-title">Warning Signals</h3>
            <div className="insights-findings-list insights-warning-list">
              {mediumFindings.map(finding => {
                const explanation = explanations.find(e => String(e.finding_id) === String(finding.id))
                const findingText = explanation?.plain_english ?? finding.body ?? ''
                return (
                  <div key={finding.id} className="insight-finding insight-finding-warning">
                    <div className="insight-finding-header">
                      <span className="insight-finding-title">
                        {explanation?.title ?? finding.title ?? finding.type}
                      </span>
                      {finding.column && (
                        <span className="insight-finding-column">{finding.column}</span>
                      )}
                    </div>
                    {findingText && <p className="insight-finding-body">{findingText}</p>}
                    {finding.evidence && Object.keys(finding.evidence).length > 0 && (
                      <div className="insight-evidence insight-warning-evidence">
                        {Object.entries(finding.evidence).slice(0, 4).map(([k, v]) => (
                          <div key={k} className="insight-evidence-chip">
                            <span className="insight-evidence-key">{k}: </span>
                            <span className="insight-evidence-value">{String(v)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Suggested Questions */}
        <div className="card anim-fade-up insights-follow-up-card">
          <div className="insights-follow-up-header">
            <h3 className="insights-follow-up-title">Follow-up Queries</h3>
            <span className="insights-follow-up-model">gpt-4o-mini generated</span>
          </div>
          {loading ? (
            <div style={{ textAlign: 'center', padding: '24px' }}>
              <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>Loading questions…</p>
            </div>
          ) : suggestedQuestions.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '24px' }}>
              <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>No suggested questions yet</p>
              <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', opacity: 0.7, marginTop: 4 }}>Questions will be generated after insight analysis</p>
            </div>
          ) : (
            <div className="insights-query-list">
              {suggestedQuestions.map((q, i) => (
                <button key={i} onClick={() => handleSuggestedQuestion(q.question)}
                  className="insights-query-row"
                  style={{ borderBottom: i < suggestedQuestions.length - 1 ? '1px solid var(--color-border-light)' : 'none' }}
                  onMouseEnter={e => e.currentTarget.style.background = 'var(--color-primary-50)'}
                  onMouseLeave={e => e.currentTarget.style.background = 'white'}
                >
                  <div className="insights-query-content">
                    <span className="insights-query-text">{q.question}</span>
                    {q.intent && (
                      <span className="insights-query-intent">
                        {q.intent}
                      </span>
                    )}
                  </div>
                  <svg className="insights-query-arrow" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>
                  </svg>
                </button>
              ))}
            </div>
          )}
        </div>

      </div>
    </Layout>
  )
}
