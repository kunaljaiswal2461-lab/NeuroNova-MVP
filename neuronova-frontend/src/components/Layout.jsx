import { useState } from 'react'
import Sidebar from './Sidebar'
import '../index.css'

export default function Layout({ title, subtitle, actions, children }) {
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false)

  return (
    <div className="app-layout">
      {/* Mobile Overlay Background */}
      <div 
        className={`mobile-overlay ${isMobileMenuOpen ? 'active' : ''}`}
        onClick={() => setIsMobileMenuOpen(false)}
      />
      
      {/* FIX 1: Pass the state down to the Sidebar */}
      <Sidebar 
        isMobileMenuOpen={isMobileMenuOpen} 
        setIsMobileMenuOpen={setIsMobileMenuOpen} 
      />
      
      <div className="main-content">
        {/* Top Bar */}
        <header className="topbar">
          {/* FIX 2: Added a wrapper div to group the Hamburger and Titles */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            
            {/* Hamburger Button (Hidden on Desktop via CSS) */}
            <button 
              className="hamburger-btn"
              onClick={() => setIsMobileMenuOpen(true)}
              style={{ 
                display: 'flex', alignItems: 'center', justifyContent: 'center', 
                width: 36, height: 36, borderRadius: 8, border: '1px solid var(--color-border)', 
                background: 'white', cursor: 'pointer', color: 'var(--color-text-primary)' 
              }}
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="3" y1="12" x2="21" y2="12" />
                <line x1="3" y1="6" x2="21" y2="6" />
                <line x1="3" y1="18" x2="21" y2="18" />
              </svg>
            </button>

            <div>
              <h1 style={{ fontFamily: 'var(--font-heading)', fontSize: 18, fontWeight: 700, color: 'var(--color-text-primary)', letterSpacing: '-0.01em' }}>
                {title}
              </h1>
              {subtitle && (
                <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-muted)', marginTop: 1 }}>
                  {subtitle}
                </p>
              )}
            </div>
          </div>
          
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {actions}
            {/* Bell */}
            <button style={{ width: 36, height: 36, borderRadius: 8, border: '1px solid var(--color-border)', background: 'white', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', color: 'var(--color-text-secondary)', transition: 'background 150ms' }}
              onMouseEnter={e => e.currentTarget.style.background = 'var(--color-base)'}
              onMouseLeave={e => e.currentTarget.style.background = 'white'}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
                <path d="M13.73 21a2 2 0 0 1-3.46 0" />
              </svg>
            </button>
            {/* Avatar */}
            <div style={{ width: 32, height: 32, borderRadius: '50%', background: 'linear-gradient(135deg, #3525CD, #0D9488)', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer' }}>
              <span style={{ fontFamily: 'var(--font-heading)', fontSize: 12, fontWeight: 700, color: 'white' }}>N</span>
            </div>
          </div>
        </header>

        {/* Page Content */}
        <main
          className="page-area dot-grid"
          style={{ minHeight: 'calc(100vh - var(--topbar-height))' }}
        >
          {children}
        </main>
      </div>
    </div>
  )
}