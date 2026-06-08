import { useState, useRef, useEffect, useCallback } from 'react'
import { useLocation } from 'react-router-dom'
import Layout from '../components/Layout'
import { api } from '../api/client'
import { useDataset } from '../context/DatasetContext'

const SUGGESTED_ACTIONS = [
  { label: 'Generate PDF Summary', style: 'neutral' },
  { label: 'Export Query to Dashboard', style: 'neutral' },
  { label: 'Clear Session Context', style: 'danger' },
]

function ModeChip({ mode, conf, routedBy }) {
  const config = {
    RAG:   { bg: '#ECFDF5', color: '#065F46', border: '#A7F3D0', label: '◫ RAG MODE' },
    QUERY: { bg: '#FFFBEB', color: '#92400E', border: '#FDE68A', label: '◈ QUERY MODE' },
  }[mode] ?? { bg: '#F3F4F6', color: '#374151', border: '#E5E7EB', label: mode }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
      <span style={{ background: config.bg, color: config.color, border: `1px solid ${config.border}`, borderRadius: 'var(--radius-full)', padding: '2px 8px', fontFamily: 'var(--font-data)', fontSize: 10, fontWeight: 500 }}>
        {config.label}
      </span>
      {conf !== undefined && (
        <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>Conf: {Math.round(conf * 100)}%</span>
      )}
      {routedBy && (
        <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>via {routedBy}</span>
      )}
    </div>
  )
}

function QueryResultTable({ result }) {
  if (!result || result.error) {
    return result?.error ? (
      <div style={{ background: '#FFF1F2', border: '1px solid #FECDD3', borderRadius: 6, padding: '8px 12px', fontFamily: 'var(--font-data)', fontSize: 12, color: '#9F1239', marginBottom: 8 }}>
        Query error: {result.error}
      </div>
    ) : null
  }
  const { columns, rows, row_count, truncated, elapsed_ms } = result
  return (
    <div style={{ overflowX: 'auto', marginBottom: 8 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr style={{ background: 'var(--color-base)' }}>
            {(columns ?? []).map(h => (
              <th key={h} style={{ padding: '6px 10px', fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', textAlign: 'left', borderBottom: '1px solid var(--color-border)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row, ri) => (
            <tr key={ri} style={{ borderBottom: '1px solid var(--color-border-light)' }}>
              {row.map((cell, ci) => (
                <td key={ci} style={{ padding: '6px 10px', fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-primary)' }}>{String(cell ?? '—')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ display: 'flex', gap: 12, marginTop: 4 }}>
        <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{row_count ?? 0} rows{truncated ? ' (truncated)' : ''}</span>
        {elapsed_ms !== undefined && (
          <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>{elapsed_ms}ms</span>
        )}
      </div>
    </div>
  )
}

export default function Chat() {
  const location = useLocation()
  const { activeDatasetId, activeDataset } = useDataset()
  const [sessionId, setSessionId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState(location.state?.prefill || '')
  const [autoMode, setAutoMode] = useState(true)
  const [streaming, setStreaming] = useState(false)
  const [sessionError, setSessionError] = useState(null)
  const [tokenCount, setTokenCount] = useState(0)
  const messagesEndRef = useRef(null)
  const textareaRef = useRef(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Create session when dataset becomes available
  useEffect(() => {
    if (!activeDatasetId) return
    setSessionId(null)
    setMessages([])
    setSessionError(null)
    api.post(`/datasets/${activeDatasetId}/chat/sessions`, { mode: 'AUTO' })
      .then(res => setSessionId(res.session?.session_id))
      .catch(err => setSessionError(err.message))
  }, [activeDatasetId])

  const handleSend = useCallback(async () => {
    if (!input.trim() || streaming) return
    if (!sessionId) {
      setSessionError('No session — select a completed dataset first.')
      return
    }

    const userMsg = { id: Date.now(), role: 'user', text: input }
    const assistantId = Date.now() + 1
    const assistantMsg = { id: assistantId, role: 'assistant', text: '', mode: null, conf: null, citations: [], queryResult: null, streaming: true }

    setMessages(prev => [...prev, userMsg, assistantMsg])
    setInput('')
    setStreaming(true)

    try {
      const stream = api.streamSSE(`/chat/sessions/${sessionId}/message`, { message: input })
      for await (const { event, data } of stream) {
        if (event === 'intent') {
          setMessages(prev => prev.map(m => m.id === assistantId
            ? { ...m, mode: data.mode, conf: data.confidence, routedBy: data.routed_by }
            : m
          ))
        } else if (event === 'citations') {
          setMessages(prev => prev.map(m => m.id === assistantId
            ? { ...m, citations: data }
            : m
          ))
        } else if (event === 'query_result') {
          setMessages(prev => prev.map(m => m.id === assistantId
            ? { ...m, queryResult: data }
            : m
          ))
        } else if (event === 'token') {
          setMessages(prev => prev.map(m => m.id === assistantId
            ? { ...m, text: m.text + (data.text ?? '') }
            : m
          ))
          setTokenCount(t => t + 1)
        } else if (event === 'done') {
          setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, streaming: false } : m))
          break
        } else if (event === 'error') {
          setMessages(prev => prev.map(m => m.id === assistantId
            ? { ...m, text: m.text || `Error: ${data.message ?? 'unknown'}`, streaming: false }
            : m
          ))
          break
        }
      }
    } catch (err) {
      setMessages(prev => prev.map(m => m.id === assistantId
        ? { ...m, text: `Connection error: ${err.message}`, streaming: false }
        : m
      ))
    } finally {
      setStreaming(false)
    }
  }, [input, streaming, sessionId])

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
  }

  const noDataset = !activeDatasetId

  return (
    <Layout title="Conversational Analyst" subtitle="Dual-mode AI: RAG insights + direct data queries">
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 260px', gap: 0, height: 'calc(100vh - var(--topbar-height) - 48px)', position: 'relative' }}>

        {/* ── Chat Area ── */}
        <div style={{ display: 'flex', flexDirection: 'column', background: 'transparent' }}>
          {/* Mode Badge */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8, marginBottom: 12, paddingRight: 12 }}>
            <button
              onClick={() => setAutoMode(!autoMode)}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                background: autoMode ? 'var(--color-primary-50)' : 'var(--color-base)',
                border: `1px solid ${autoMode ? '#BFDBFE' : 'var(--color-border)'}`,
                borderRadius: 'var(--radius-full)', padding: '5px 12px', cursor: 'pointer',
                transition: 'all 150ms',
              }}
            >
              <div style={{ width: 6, height: 6, borderRadius: '50%', background: autoMode ? '#2563EB' : 'var(--color-text-muted)', position: 'relative' }}>
                {autoMode && <div style={{ position: 'absolute', inset: -2, borderRadius: '50%', background: '#2563EB', opacity: 0.3, animation: 'ripple 1.5s ease-out infinite' }} />}
              </div>
              <span style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: autoMode ? '#1D4ED8' : 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                {autoMode ? 'AUTO MODE' : 'MANUAL'}
              </span>
            </button>
          </div>

          {/* Messages */}
          <div style={{ flex: 1, overflowY: 'auto', paddingRight: 12, display: 'flex', flexDirection: 'column', gap: 16 }}>
            {sessionError && (
              <div style={{ background: '#FFF1F2', border: '1px solid #FECDD3', borderRadius: 8, padding: '12px 16px', fontFamily: 'var(--font-body)', fontSize: 13, color: '#9F1239' }}>
                {sessionError}
              </div>
            )}

            {messages.length === 0 ? (
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center', padding: '40px 24px', gap: 12 }}>
                <div style={{ width: 52, height: 52, borderRadius: '50%', background: '#F0EDF9', border: '1px solid #DDD9F5', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--color-primary-indigo)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
                  </svg>
                </div>
                {noDataset ? (
                  <>
                    <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)' }}>No dataset selected</h3>
                    <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', maxWidth: 340, lineHeight: 1.6 }}>
                      Select a profiled dataset from the Upload Center to start chatting.
                    </p>
                    <a href="/upload" className="btn btn-primary btn-sm" style={{ marginTop: 8 }}>Go to Upload →</a>
                  </>
                ) : !sessionId ? (
                  <>
                    <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)' }}>Creating session…</h3>
                    <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', maxWidth: 340, lineHeight: 1.6 }}>
                      Connecting to {activeDataset?.original_name ?? 'your dataset'}
                    </p>
                  </>
                ) : (
                  <>
                    <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)' }}>Start a conversation</h3>
                    <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', maxWidth: 340, lineHeight: 1.6 }}>
                      Ask a question about your dataset or request a query. The analyst automatically routes between RAG retrieval and direct data queries.
                    </p>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'center', marginTop: 8 }}>
                      {['Summarize this dataset', 'Show null distribution', 'Find outliers in revenue'].map(hint => (
                        <button key={hint} onClick={() => setInput(hint)}
                          style={{ background: 'white', border: '1px solid var(--color-border)', borderRadius: 'var(--radius-full)', padding: '6px 14px', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)', cursor: 'pointer', transition: 'all 150ms' }}
                          onMouseEnter={e => { e.currentTarget.style.background = 'var(--color-primary-50)'; e.currentTarget.style.color = 'var(--color-primary-indigo)' }}
                          onMouseLeave={e => { e.currentTarget.style.background = 'white'; e.currentTarget.style.color = 'var(--color-text-secondary)' }}
                        >{hint}</button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            ) : (
              messages.map(msg => (
                <div key={msg.id} className="anim-fade-up">
                  {msg.role === 'user' ? (
                    <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                      <div className="chat-user">{msg.text}</div>
                    </div>
                  ) : (
                    <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
                      <div style={{ width: 28, height: 28, borderRadius: 8, background: 'var(--color-primary)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, marginTop: 4 }}>
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <circle cx="12" cy="12" r="3"/><path d="M3 12h3M18 12h3M12 3v3M12 18v3"/>
                        </svg>
                      </div>
                      <div style={{ flex: 1, maxWidth: '85%' }}>
                        <div className="chat-assistant">
                          {msg.mode && <ModeChip mode={msg.mode} conf={msg.conf} routedBy={msg.routedBy} />}
                          <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)', lineHeight: 1.6, marginBottom: (msg.citations?.length || msg.queryResult) ? 12 : 0, whiteSpace: 'pre-wrap' }}>
                            {msg.text}
                            {msg.streaming && <span style={{ display: 'inline-block', width: 8, height: 14, background: 'var(--color-primary-indigo)', marginLeft: 2, borderRadius: 1, verticalAlign: 'text-bottom', animation: 'pulse-dot 0.8s ease-in-out infinite' }} />}
                          </p>
                          {msg.queryResult && <QueryResultTable result={msg.queryResult} />}
                          {msg.citations?.length > 0 && (
                            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                              {msg.citations.map((c, ci) => (
                                <span key={ci} style={{ display: 'flex', alignItems: 'center', gap: 4, background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 4, padding: '2px 8px' }}>
                                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="var(--color-text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
                                  </svg>
                                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-secondary)' }} title={`${c.finding_type} · ${c.severity}`}>
                                    {c.title ?? c.finding_id ?? `Finding ${ci + 1}`}
                                  </span>
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              ))
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Input Area */}
          <div style={{ marginTop: 12, background: 'white', border: '1px solid var(--color-border)', borderRadius: 12, padding: '12px 14px', boxShadow: 'var(--shadow-card)' }}>
            <textarea
              ref={textareaRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={noDataset ? 'Select a dataset to start chatting…' : !sessionId ? 'Creating session…' : 'Ask a question or request a query…'}
              rows={2}
              disabled={noDataset || !sessionId || streaming}
              style={{
                width: '100%', border: 'none', outline: 'none', resize: 'none',
                fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)',
                lineHeight: 1.5, background: 'transparent',
                opacity: (noDataset || !sessionId) ? 0.5 : 1,
              }}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--color-border-light)' }}>
              <span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>
                {streaming ? '⟳ Streaming response…' : autoMode ? '⊙ Auto Mode active · Routes between RAG or DB Query.' : 'Manual mode'}
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)' }}>Return to send</span>
                <button
                  onClick={handleSend}
                  disabled={!input.trim() || noDataset || !sessionId || streaming}
                  style={{
                    width: 32, height: 32, borderRadius: 8,
                    background: (input.trim() && sessionId && !streaming) ? 'var(--color-primary-indigo)' : 'var(--color-border)',
                    border: 'none', display: 'flex', alignItems: 'center', justifyContent: 'center',
                    cursor: (input.trim() && sessionId && !streaming) ? 'pointer' : 'not-allowed',
                    transition: 'background 150ms',
                  }}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* ── Session Context Panel ── */}
        <div style={{ paddingLeft: 16, borderLeft: '1px solid var(--color-border)', overflowY: 'auto' }}>
          <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 14, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 14 }}>Session Context</h3>

          {/* Active Data Sources */}
          <div style={{ marginBottom: 20 }}>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Active Data Sources</p>
            {!activeDataset ? (
              <div style={{ background: 'var(--color-base)', border: '1px solid var(--color-border)', borderRadius: 8, padding: '10px 12px', textAlign: 'center' }}>
                <span style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', fontStyle: 'italic' }}>No dataset connected</span>
              </div>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, background: 'white', border: '1px solid var(--color-border)', borderRadius: 8, padding: '8px 10px' }}>
                <div style={{ width: 22, height: 22, borderRadius: 4, background: '#F0EDF9', display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-primary-indigo)' }}>
                  {(activeDataset.file_type ?? 'CSV').slice(0, 3).toUpperCase()}
                </div>
                <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {activeDataset.original_name}
                </span>
              </div>
            )}
          </div>

          {/* Compute Engine */}
          <div style={{ marginBottom: 20 }}>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Compute Engine</p>
            <div style={{ background: 'white', border: '1px solid var(--color-border)', borderRadius: 8, padding: '10px 12px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <span style={{ fontFamily: 'var(--font-heading)', fontSize: 13, fontWeight: 600, color: 'var(--color-text-primary)' }}>gpt-4o + pgvector</span>
                <div style={{ width: 6, height: 6, borderRadius: '50%', background: sessionId ? 'var(--color-success)' : 'var(--color-text-muted)' }} />
              </div>
              <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>
                {sessionId ? 'Session active' : 'No session'}
              </span>
            </div>
          </div>

          {/* Suggested Actions */}
          <div>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Suggested Actions</p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {SUGGESTED_ACTIONS.map((a, i) => (
                <button key={i}
                  onClick={() => {
                    if (a.label === 'Clear Session Context' && sessionId) {
                      api.post(`/datasets/${activeDatasetId}/chat/sessions`, { mode: 'AUTO' })
                        .then(res => { setSessionId(res.session?.session_id); setMessages([]) })
                        .catch(console.error)
                    }
                  }}
                  style={{
                    padding: '8px 12px', background: 'white',
                    border: `1px solid ${a.style === 'danger' ? 'var(--color-danger-border)' : 'var(--color-border)'}`,
                    borderRadius: 8, fontFamily: 'var(--font-body)', fontSize: 12, fontWeight: 500,
                    color: a.style === 'danger' ? 'var(--color-danger)' : 'var(--color-text-primary)',
                    cursor: 'pointer', textAlign: 'left', transition: 'background 150ms',
                  }}
                  onMouseEnter={e => e.currentTarget.style.background = a.style === 'danger' ? 'var(--color-danger-bg)' : 'var(--color-base)'}
                  onMouseLeave={e => e.currentTarget.style.background = 'white'}
                >
                  {a.label}
                </button>
              ))}
            </div>
          </div>

          {/* Session Stats */}
          <div style={{ marginTop: 20, padding: '12px', background: 'var(--color-base)', borderRadius: 8 }}>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Session Stats</p>
            {[
              ['Messages', String(messages.length)],
              ['Tokens', `~${tokenCount}`],
              ['Mode', 'AUTO'],
              ['Session ID', sessionId ? sessionId.slice(0, 8) + '…' : '—'],
            ].map(([k, v]) => (
              <div key={k} style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                <span style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)' }}>{k}</span>
                <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-primary)' }}>{v}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </Layout>
  )
}
