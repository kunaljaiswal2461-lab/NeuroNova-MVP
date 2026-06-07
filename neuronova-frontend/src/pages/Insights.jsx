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
      <Layout title="AI Insights" subtitle="LLM-powered analysis grounded in your dataset findings">
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
      <div style={{ maxWidth: 820, display: 'flex', flexDirection: 'column', gap: 20 }}>

        {error && (
          <div style={{ background: '#FFF1F2', border: '1px solid #FECDD3', borderRadius: 8, padding: '12px 16px', fontFamily: 'var(--font-body)', fontSize: 13, color: '#9F1239' }}>
            {error}
          </div>
        )}

        {/* Tags */}
        {tags.length > 0 && (
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {tags.map((tag, i) => (
              <span key={i} className="tag">{tag}</span>
            ))}
          </div>
        )}

        {/* Executive Summary */}
        {loading ? (
          <div className="card card-pad anim-fade-up" style={{ padding: '32px', textAlign: 'center' }}>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>Loading AI insights…</p>
          </div>
        ) : !exec ? (
          <div className="card card-pad anim-fade-up" style={{ textAlign: 'center', padding: '48px 24px' }}>
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
          <div className="card card-pad anim-fade-up">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
              <p className="text-label-caps" style={{ color: 'var(--color-primary-indigo)' }}>Executive Summary</p>
              <span style={{ background: '#F5F3FF', border: '1px solid #DDD9F5', color: 'var(--color-primary-indigo)', borderRadius: 'var(--radius-full)', padding: '3px 10px', fontFamily: 'var(--font-data)', fontSize: 11 }}>
                ● {insights?.model_used ?? 'gpt-4o'}
              </span>
            </div>
            <h2 style={{ fontFamily: 'var(--font-heading)', fontSize: 20, fontWeight: 600, color: 'var(--color-text-primary)', lineHeight: 1.35, marginBottom: 12 }}>
              {exec.headline}
            </h2>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'var(--color-text-secondary)', lineHeight: 1.7, marginBottom: exec.recommended_next_steps?.length ? 16 : 0 }}>
              {exec.overview}
            </p>
            {exec.recommended_next_steps?.length > 0 && (
              <div style={{ background: 'var(--color-base)', borderRadius: 8, padding: '12px 16px' }}>
                <p style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>Next Steps</p>
                {exec.recommended_next_steps.map((step, i) => (
                  <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 4 }}>
                    <span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-primary-indigo)', fontWeight: 600 }}>{i + 1}.</span>
                    <span style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', lineHeight: 1.5 }}>{step}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* High-Severity Findings */}
        {highFindings.length > 0 && (
          <div>
            <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 12 }}>
              High-Severity Findings
            </h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {highFindings.map(finding => {
                const explanation = explanations.find(e => String(e.finding_id) === String(finding.id))
                return (
                  <div key={finding.id} className="anim-fade-up" style={{
                    background: 'white',
                    border: '1px solid var(--color-danger-border)',
                    borderLeft: '4px solid var(--color-danger)',
                    borderRadius: '0 12px 12px 0',
                    padding: '16px 16px 16px 20px',
                    boxShadow: 'var(--shadow-card)',
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-danger)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                          <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
                        </svg>
                        <span style={{ fontFamily: 'var(--font-heading)', fontSize: 14, fontWeight: 600, color: 'var(--color-text-primary)' }}>
                          {explanation?.title ?? finding.title ?? finding.type}
                        </span>
                      </div>
                      {finding.column && (
                        <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>
                          Col: {finding.column}
                        </span>
                      )}
                    </div>

                    <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', lineHeight: 1.6, marginBottom: 12 }}>
                      {explanation?.plain_english ?? finding.body ?? ''}
                    </p>

                    {/* Evidence chips */}
                    {finding.evidence && Object.keys(finding.evidence).length > 0 && (
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
                        {Object.entries(finding.evidence).slice(0, 6).map(([k, v]) => (
                          <div key={k} style={{ background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 4, padding: '3px 8px' }}>
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{k}: </span>
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-primary)', fontWeight: 500 }}>{String(v)}</span>
                          </div>
                        ))}
                        <div style={{ background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 4, padding: '3px 8px' }}>
                          <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>confidence: </span>
                          <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-primary)', fontWeight: 500 }}>{((finding.confidence ?? 0) * 100).toFixed(0)}%</span>
                        </div>
                      </div>
                    )}

                    {/* Recommended Action */}
                    {explanation?.suggested_action && (
                      <div style={{ background: 'var(--color-primary-50)', border: '1px solid #BFDBFE', borderRadius: 8, padding: '10px 12px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--color-info)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                          </svg>
                          <span style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'var(--color-info)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Recommended Action</span>
                        </div>
                        <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-info)', lineHeight: 1.5 }}>
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
          <div>
            <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 12 }}>Warning Signals</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {mediumFindings.map(finding => {
                const explanation = explanations.find(e => String(e.finding_id) === String(finding.id))
                return (
                  <div key={finding.id} style={{
                    background: 'white',
                    border: '1px solid var(--color-warning-border)',
                    borderLeft: '4px solid var(--color-warning)',
                    borderRadius: '0 12px 12px 0',
                    padding: '14px 16px 14px 20px',
                    boxShadow: 'var(--shadow-card)',
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                      <span style={{ fontFamily: 'var(--font-heading)', fontSize: 13, fontWeight: 600, color: 'var(--color-text-primary)' }}>
                        {explanation?.title ?? finding.title ?? finding.type}
                      </span>
                      {finding.column && (
                        <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>{finding.column}</span>
                      )}
                    </div>
                    <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', lineHeight: 1.5, marginBottom: 8 }}>
                      {explanation?.plain_english ?? finding.body ?? ''}
                    </p>
                    {finding.evidence && Object.keys(finding.evidence).length > 0 && (
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        {Object.entries(finding.evidence).slice(0, 4).map(([k, v]) => (
                          <div key={k} style={{ background: 'var(--color-warning-bg)', border: '1px solid var(--color-warning-border)', borderRadius: 4, padding: '2px 8px' }}>
                            <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-warning)' }}>{k}: {String(v)}</span>
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
        <div className="card anim-fade-up">
          <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--color-border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 15, fontWeight: 600, color: 'var(--color-text-primary)' }}>Follow-up Queries</h3>
            <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>gpt-4o-mini generated</span>
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
            <div style={{ display: 'flex', flexDirection: 'column' }}>
              {suggestedQuestions.map((q, i) => (
                <button key={i} onClick={() => handleSuggestedQuestion(q.question)}
                  style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '13px 20px', borderBottom: i < suggestedQuestions.length - 1 ? '1px solid var(--color-border-light)' : 'none', background: 'white', cursor: 'pointer', textAlign: 'left', transition: 'background 150ms' }}
                  onMouseEnter={e => e.currentTarget.style.background = 'var(--color-primary-50)'}
                  onMouseLeave={e => e.currentTarget.style.background = 'white'}
                >
                  <div style={{ flex: 1 }}>
                    <span style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)', lineHeight: 1.4 }}>{q.question}</span>
                    {q.intent && (
                      <span style={{ display: 'inline-block', marginLeft: 8, fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 4, padding: '1px 5px' }}>
                        {q.intent}
                      </span>
                    )}
                  </div>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginLeft: 12 }}>
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
