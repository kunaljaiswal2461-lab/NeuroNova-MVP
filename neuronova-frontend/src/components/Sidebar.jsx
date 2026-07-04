import { useState } from 'react'
import { NavLink, Link } from 'react-router-dom'
import { useDataset } from '../context/DatasetContext'

const NAV_ITEMS = [
  {
    to: '/upload',
    label: 'Upload',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="16 16 12 12 8 16" /><line x1="12" y1="12" x2="12" y2="21" />
        <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" />
      </svg>
    ),
  },
  {
    to: '/explorer',
    label: 'Explorer',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
      </svg>
    ),
  },
  {
    to: '/visualization',
    label: 'Visualization',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" />
        <line x1="6" y1="20" x2="6" y2="14" />
      </svg>
    ),
  },
  {
    to: '/insights',
    label: 'Insights',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
      </svg>
    ),
  },
  {
    to: '/chat',
    label: 'Chat',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
      </svg>
    ),
  },
]

export default function Sidebar() {
  const { activeDataset } = useDataset()
  const [isSupportOpen, setIsSupportOpen] = useState(false) // Added state for Support Menu

  const health = activeDataset?._health ?? null

  return (
    <aside className="sidebar">
      {/* Wordmark */}
      <div style={{ padding: '20px 16px 12px' }}>
        {/* Changed this wrapper to a Link pointing to '/' */}
          <Link to="/" style={{ display: 'flex', alignItems: 'center', gap: 8, textDecoration: 'none', cursor: 'pointer' }}>
            <img 
              src="/logo.png" 
              alt="NeuroNova Logo" 
              style={{ width: 28, height: 28, borderRadius: 6, flexShrink: 0, objectFit: 'cover' }} 
            />
            <div>
              <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 15, color: 'white', letterSpacing: '-0.01em' }}>
                NeuroNova
              </div>
            </div>
          </Link>

        {/* Active Dataset Chip */}
        <div style={{
          marginTop: 12,
          background: 'rgba(255,255,255,0.07)',
          border: '1px solid rgba(255,255,255,0.12)',
          borderRadius: 8,
          padding: '8px 10px',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          cursor: 'pointer',
          transition: 'background 150ms',
        }}
          onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.11)'}
          onMouseLeave={e => e.currentTarget.style.background = 'rgba(255,255,255,0.07)'}
        >
          <div style={{ width: 22, height: 22, borderRadius: 4, background: 'rgba(79,70,229,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
              <rect x="3" y="3" width="18" height="18" rx="2" /><line x1="3" y1="9" x2="21" y2="9" /><line x1="3" y1="15" x2="21" y2="15" /><line x1="9" y1="3" x2="9" y2="21" />
            </svg>
          </div>
          <div style={{ overflow: 'hidden' }}>
            {activeDataset ? (
              <>
                <div style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'rgba(255,255,255,0.85)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {activeDataset.original_name}
                </div>
                <div style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'rgba(255,255,255,0.35)', marginTop: 2 }}>
                  {activeDataset.row_count?.toLocaleString() ?? '—'} rows · {activeDataset.col_count ?? '—'} cols
                </div>
              </>
            ) : (
              <>
                <div style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'rgba(255,255,255,0.45)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', fontStyle: 'italic' }}>
                  No dataset loaded
                </div>
                <div style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'rgba(255,255,255,0.25)', marginTop: 2 }}>
                  Upload a file to begin
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Nav Label */}
      <div style={{ padding: '4px 16px 6px', fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: 'rgba(255,255,255,0.3)', letterSpacing: '0.08em', textTransform: 'uppercase' }}>
        Workspace
      </div>

      {/* Navigation */}
      <nav style={{ padding: '0 8px', flex: 1 }}>
        {NAV_ITEMS.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
            style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', marginBottom: 2, borderRadius: 8, textDecoration: 'none', color: 'rgba(255,255,255,0.65)', fontSize: 14, fontFamily: 'var(--font-body)', fontWeight: 500, cursor: 'pointer', transition: 'all 150ms' }}
          >
            <span style={{ width: 16, height: 16, flexShrink: 0 }}>{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
      </nav>

      {/* Health Score Card */}
      <div style={{ padding: '8px 12px 12px' }}>
        <div className="health-card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <div style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 600, color: 'rgba(255,255,255,0.7)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Dataset Health
            </div>
          </div>
          {!health ? (
            <div style={{ textAlign: 'center', padding: '12px 0' }}>
              <div style={{ fontFamily: 'var(--font-display)', fontSize: 28, fontWeight: 700, color: 'rgba(255,255,255,0.2)', lineHeight: 1 }}>—</div>
              <div style={{ fontFamily: 'var(--font-body)', fontSize: 10, color: 'rgba(255,255,255,0.3)', marginTop: 6 }}>No dataset profiled yet</div>
            </div>
          ) : (
            <>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 4, marginBottom: 12 }}>
                <span style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 700, color: 'white', lineHeight: 1 }}>{Math.round(health.score)}</span>
                <span style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'rgba(255,255,255,0.4)' }}>/ 100</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {Object.entries(health.components).map(([key, val]) => (
                  <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{ fontFamily: 'var(--font-body)', fontSize: 10, color: 'rgba(255,255,255,0.5)', width: 68, flexShrink: 0, textTransform: 'capitalize' }}>{key}</div>
                    <div style={{ flex: 1, height: 4, background: 'rgba(255,255,255,0.1)', borderRadius: 2, overflow: 'hidden' }}>
                      <div style={{ height: '100%', width: `${val}%`, background: val >= 80 ? '#34D399' : val >= 60 ? '#FBBF24' : '#F87171', borderRadius: 2, transition: 'width 600ms cubic-bezier(0.25,1,0.5,1)' }} />
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Bottom Actions - Unrolled to add dropdown logic */}
      <div style={{ borderTop: '1px solid rgba(255,255,255,0.08)', padding: '8px 8px 12px', display: 'flex', flexDirection: 'column', gap: '2px' }}>
        
        {/* Settings Button */}
        <button className="nav-item" style={{ fontSize: 13, gap: 8, display: 'flex', alignItems: 'center', width: '100%', background: 'transparent', border: 'none', color: 'rgba(255,255,255,0.65)', padding: '8px 12px', borderRadius: '8px', cursor: 'pointer' }}>
          <span style={{ width: 15, height: 15, flexShrink: 0 }}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
          </span>
          Settings
        </button>

        {/* Support Button & Dropdown */}
        <div style={{ position: 'relative' }}>
          {isSupportOpen && (
            <div style={{
              position: 'absolute',
              bottom: '100%',
              left: 0,
              marginBottom: 8,
              width: '100%',
              minWidth: '160px',
              background: 'rgba(15, 23, 42, 0.95)', // matches dark dashboard theme
              backdropFilter: 'blur(10px)',
              border: '1px solid rgba(255,255,255,0.1)',
              borderRadius: 8,
              padding: '4px 0',
              zIndex: 50,
              boxShadow: '0 10px 25px -5px rgba(0, 0, 0, 0.5)'
            }}>
              <Link 
                to="/" 
                style={{ display: 'block', padding: '8px 16px', fontSize: 13, color: 'white', textDecoration: 'none', fontFamily: 'var(--font-body)', transition: 'background 150ms' }}
                onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.1)'}
                onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
              >
                Explore Website
              </Link>
              <div style={{ display: 'block', padding: '8px 16px', fontSize: 13, color: 'rgba(255,255,255,0.3)', cursor: 'not-allowed', fontFamily: 'var(--font-body)' }}>
                Email Support (Soon)
              </div>
            </div>
          )}

          <button 
            className="nav-item" 
            onClick={() => setIsSupportOpen(!isSupportOpen)}
            style={{ fontSize: 13, gap: 8, display: 'flex', alignItems: 'center', width: '100%', background: 'transparent', border: 'none', color: 'rgba(255,255,255,0.65)', padding: '8px 12px', borderRadius: '8px', cursor: 'pointer' }}
          >
            <span style={{ width: 15, height: 15, flexShrink: 0 }}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            </span>
            Support
          </button>
        </div>

      </div>
    </aside>
  )
}