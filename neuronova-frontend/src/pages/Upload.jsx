import { useState, useRef, useEffect, memo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import Layout from '../components/Layout'
import { api } from '../api/client'
import { useDataset } from '../context/DatasetContext'

const STATUS_CONFIG = {
  COMPLETE:  { bg: '#ECFDF5', color: '#065F46', border: '#A7F3D0' },
  FAILED:    { bg: '#FFF1F2', color: '#9F1239', border: '#FECDD3' },
  UPLOADED:  { bg: '#F0F9FF', color: '#075985', border: '#BAE6FD' },
  QUEUED:    { bg: '#F0F9FF', color: '#075985', border: '#BAE6FD' },
  PROFILING: { bg: '#FFFBEB', color: '#92400E', border: '#FDE68A' },
  FINDINGS:  { bg: '#FFFBEB', color: '#92400E', border: '#FDE68A' },
  VIZ:       { bg: '#FFFBEB', color: '#92400E', border: '#FDE68A' },
  INSIGHTS:  { bg: '#FFFBEB', color: '#92400E', border: '#FDE68A' },
  INDEXING:  { bg: '#FFFBEB', color: '#92400E', border: '#FDE68A' },
}

const PIPELINE_STEPS = [
  { key: 'UPLOADED',  label: 'Dataset registered' },
  { key: 'QUEUED',    label: 'Queued for processing' },
  { key: 'PROFILING', label: 'Running profiler (schema, stats, quality…)' },
  { key: 'FINDINGS',  label: 'Extracting findings' },
  { key: 'VIZ',       label: 'Building visualization metadata' },
  { key: 'INSIGHTS',  label: 'Generating LLM insights' },
  { key: 'INDEXING',  label: 'Indexing semantic embeddings' },
  { key: 'COMPLETE',  label: 'Pipeline complete' },
]

function StepIcon({ status }) {
  if (status === 'complete') return (
    <div className="step-icon step-complete">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="20 6 9 17 4 12" />
      </svg>
    </div>
  )
  if (status === 'active') return (
    <div className="step-icon step-active" style={{ animation: 'none' }}>
      <div style={{ width: 8, height: 8, borderRadius: '50%', background: 'white' }} />
    </div>
  )
  return <div className="step-icon step-pending" style={{ fontSize: 11 }}>—</div>
}

function formatBytes(bytes) {
  if (!bytes) return '—'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function timeAgo(dateStr) {
  if (!dateStr) return '—'
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

// Memoized so the 2s pipeline-status poll (which only updates pipelineStatus /
// progressPct) does not re-render this whole table on every tick. It re-renders
// only when the datasets array or the callbacks actually change.
const RecentDatasets = memo(function RecentDatasets({ datasets, onRefresh, onOpen }) {
  return (
    <div className="card">
      <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--color-border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 15, fontWeight: 600, color: 'var(--color-text-primary)' }}>Recent Datasets</h3>
        <button className="btn btn-ghost-neutral btn-sm" onClick={onRefresh}>Refresh</button>
      </div>

      <table className="data-table" style={{ width: '100%' }}>
        <thead>
          <tr>
            <th>Name</th>
            <th>Rows</th>
            <th>Cols</th>
            <th>Status</th>
            <th>Size</th>
            <th>Uploaded</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {datasets.length === 0 ? (
            <tr>
              <td colSpan={7} style={{ textAlign: 'center', padding: '32px 16px' }}>
                <div style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)', marginBottom: 6 }}>No datasets yet</div>
                <div style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', opacity: 0.7 }}>Upload a file above to get started</div>
              </td>
            </tr>
          ) : (
            datasets.map((ds) => {
              const sc = STATUS_CONFIG[ds.status] || STATUS_CONFIG.QUEUED
              const ext = (ds.file_type || 'CSV').toUpperCase().slice(0, 4)
              return (
                <tr key={ds.dataset_id}>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      <div style={{ width: 28, height: 28, borderRadius: 6, background: '#EFF6FF', display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-primary-indigo)', flexShrink: 0 }}>
                        {ext}
                      </div>
                      <span style={{ fontFamily: 'var(--font-data)', fontSize: 13, color: 'var(--color-text-primary)' }}>{ds.original_name}</span>
                    </div>
                  </td>
                  <td><span style={{ fontFamily: 'var(--font-data)', fontSize: 13 }}>{ds.row_count?.toLocaleString() ?? '—'}</span></td>
                  <td><span style={{ fontFamily: 'var(--font-data)', fontSize: 13 }}>{ds.col_count ?? '—'}</span></td>
                  <td>
                    <span style={{ background: sc.bg, color: sc.color, border: `1px solid ${sc.border}`, borderRadius: 'var(--radius-full)', padding: '3px 10px', fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, letterSpacing: '0.05em' }}>
                      {ds.status}
                    </span>
                  </td>
                  <td><span style={{ fontFamily: 'var(--font-data)', fontSize: 12, color: 'var(--color-text-muted)' }}>{formatBytes(ds.size_bytes)}</span></td>
                  <td style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)' }}>{timeAgo(ds.uploaded_at)}</td>
                  <td>
                    {ds.status === 'COMPLETE' && (
                      <button
                        onClick={() => onOpen(ds)}
                        style={{ padding: '4px 10px', borderRadius: 6, border: '1px solid var(--color-border)', background: 'white', fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)', cursor: 'pointer', transition: 'all 150ms' }}
                        onMouseEnter={e => { e.currentTarget.style.background = 'var(--color-base)'; e.currentTarget.style.color = 'var(--color-text-primary)' }}
                        onMouseLeave={e => { e.currentTarget.style.background = 'white'; e.currentTarget.style.color = 'var(--color-text-secondary)' }}
                      >
                        Open →
                      </button>
                    )}
                  </td>
                </tr>
              )
            })
          )}
        </tbody>
      </table>
    </div>
  )
})

export default function Upload() {
  const [dragActive, setDragActive] = useState(false)
  const [datasets, setDatasets] = useState([])
  const [uploading, setUploading] = useState(false)
  const [uploadedName, setUploadedName] = useState('')
  const [pipelineStatus, setPipelineStatus] = useState(null)
  const [progressPct, setProgressPct] = useState(0)
  const [error, setError] = useState(null)
  const fileRef = useRef(null)
  const pollRef = useRef(null)
  const { selectDataset } = useDataset()
  const navigate = useNavigate()

  const fetchDatasets = useCallback(async () => {
    try {
      const data = await api.get('/datasets')
      setDatasets(data.items || [])
    } catch (err) {
      console.error('Failed to load datasets:', err)
    }
  }, [])

  const openDataset = useCallback((ds) => {
    selectDataset(ds)
    navigate('/explorer')
  }, [selectDataset, navigate])

  useEffect(() => {
    fetchDatasets()
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [fetchDatasets])

  async function uploadFile(file) {
    setError(null)
    setUploading(true)
    setUploadedName(file.name)
    setPipelineStatus('UPLOADED')
    setProgressPct(0)
    try {
      const form = new FormData()
      form.append('file', file)
      const ack = await api.postForm('/upload', form)
      startPolling(ack.dataset_id)
    } catch (err) {
      setError('Upload failed: ' + err.message)
      setUploading(false)
    }
  }

  function startPolling(id) {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const st = await api.get(`/datasets/${id}/status`)
        const pct = st.progress_pct ?? 0
        setPipelineStatus(prev => (prev === st.status ? prev : st.status))
        setProgressPct(prev => (prev === pct ? prev : pct))
        if (st.status === 'COMPLETE') {
          clearInterval(pollRef.current)
          setUploading(false)
          const ds = await api.get(`/datasets/${id}`)
          selectDataset(ds)
          fetchDatasets()
        } else if (st.status === 'FAILED') {
          clearInterval(pollRef.current)
          setUploading(false)
          setError(st.error_message || 'Pipeline failed. Check backend logs.')
          fetchDatasets()
        }
      } catch (err) {
        console.error('Poll error:', err)
      }
    }, 2000)
  }

  const handleDrop = (e) => {
    e.preventDefault()
    setDragActive(false)
    const file = e.dataTransfer.files[0]
    if (file) uploadFile(file)
  }

  const handleFileChange = (e) => {
    const file = e.target.files[0]
    if (file) uploadFile(file)
    e.target.value = ''
  }

  const handleDragOver = (e) => { e.preventDefault(); setDragActive(true) }
  const handleDragLeave = () => setDragActive(false)
  const handleClick = () => fileRef.current?.click()

  const currentIdx = PIPELINE_STEPS.findIndex(s => s.key === pipelineStatus)
  const steps = PIPELINE_STEPS.map((s, i) => ({
    ...s,
    status: i < currentIdx ? 'complete' : i === currentIdx ? 'active' : 'pending',
  }))

  return (
    <Layout
      title="Upload Center"
      subtitle="Upload a dataset to begin AI-powered analysis"
      actions={
        <button className="btn btn-primary" onClick={handleClick} style={{ gap: 6 }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          New Dataset
        </button>
      }
    >
      <div style={{ maxWidth: 860, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 20 }}>

        {error && (
          <div style={{ background: '#FFF1F2', border: '1px solid #FECDD3', borderRadius: 8, padding: '12px 16px', fontFamily: 'var(--font-body)', fontSize: 13, color: '#9F1239' }}>
            {error}
          </div>
        )}

        {/* Drop Zone */}
        <div
          className={`upload-zone ${dragActive ? 'drag-active' : ''}`}
          onDrop={handleDrop}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onClick={handleClick}
        >
          <input ref={fileRef} type="file" style={{ display: 'none' }} accept=".csv,.xlsx,.json,.parquet" onChange={handleFileChange} />

          <div style={{ width: 52, height: 52, borderRadius: '50%', background: '#EFF6FF', border: '1px solid #BFDBFE', display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 16px' }}>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--color-primary-indigo)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="16 16 12 12 8 16" /><line x1="12" y1="12" x2="12" y2="21" />
              <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" />
            </svg>
          </div>

          <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 6 }}>
            {dragActive ? 'Drop your dataset here' : uploading ? 'Processing…' : 'Drop your dataset here'}
          </h3>
          <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', marginBottom: 16 }}>
            or click to browse · max 100MB
          </p>
          <div style={{ display: 'flex', justifyContent: 'center', gap: 8 }}>
            {['CSV', 'XLSX', 'JSON', 'PARQUET'].map(fmt => (
              <span key={fmt} style={{ padding: '4px 10px', background: 'white', border: '1px solid var(--color-border)', borderRadius: 4, fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-secondary)' }}>
                {fmt}
              </span>
            ))}
          </div>
        </div>

        {/* Pipeline Progress */}
        {pipelineStatus && (
          <div className="card card-pad anim-fade-up">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <div>
                <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 15, fontWeight: 600, color: 'var(--color-text-primary)' }}>
                  Processing Pipeline
                </h3>
                <p style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)', marginTop: 2 }}>
                  {uploadedName}
                </p>
              </div>
              {(() => {
                const sc = STATUS_CONFIG[pipelineStatus] || STATUS_CONFIG.QUEUED
                return (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, background: sc.bg, border: `1px solid ${sc.border}`, borderRadius: 'var(--radius-full)', padding: '4px 12px' }}>
                    {pipelineStatus !== 'COMPLETE' && pipelineStatus !== 'FAILED' && (
                      <div style={{ width: 6, height: 6, borderRadius: '50%', background: sc.color, animation: 'pulse-dot 1.5s ease-in-out infinite' }} />
                    )}
                    <span style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: sc.color, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                      {pipelineStatus}
                    </span>
                  </div>
                )
              })()}
            </div>

            <div style={{ height: 4, background: 'var(--color-border-light)', borderRadius: 2, marginBottom: 20, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${progressPct}%`, background: pipelineStatus === 'FAILED' ? 'var(--color-danger)' : 'var(--color-primary-indigo)', borderRadius: 2, transition: 'width 600ms ease' }} />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              {steps.map((step, i) => (
                <div key={i} className="progress-step">
                  <StepIcon status={step.status} />
                  <span style={{
                    fontFamily: 'var(--font-body)', fontSize: 13,
                    color: step.status === 'active' ? 'var(--color-text-primary)'
                      : step.status === 'complete' ? 'var(--color-text-secondary)'
                        : 'var(--color-text-muted)',
                    fontWeight: step.status === 'active' ? 500 : 400,
                  }}>
                    {step.label}
                  </span>
                  {step.status === 'active' && (
                    <span style={{ marginLeft: 'auto', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-primary-indigo)' }}>Running…</span>
                  )}
                  {step.status === 'complete' && (
                    <span style={{ marginLeft: 'auto', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-success)' }}>Done</span>
                  )}
                </div>
              ))}
            </div>

            {pipelineStatus === 'COMPLETE' && (
              <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
                <button className="btn btn-primary btn-sm" onClick={() => navigate('/explorer')}>Explore Dataset →</button>
                <button className="btn btn-ghost-neutral btn-sm" onClick={() => navigate('/insights')}>View Insights</button>
              </div>
            )}
          </div>
        )}

        {/* Recent Datasets */}
        <RecentDatasets datasets={datasets} onRefresh={fetchDatasets} onOpen={openDataset} />
      </div>
    </Layout>
  )
}