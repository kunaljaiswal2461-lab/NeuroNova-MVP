import { Link } from 'react-router-dom'

const NAV_LINKS = ['Product', 'How It Works', 'Use Cases', 'Pricing']

const FEATURES = [
  {
    icon: '🧠',
    title: 'Semantic Column Intelligence',
    desc: 'NeuroNova identifies what a column represents — FINANCIAL, GEOGRAPHIC, EMAIL — not just its data type. Generic tools can\'t do this.',
  },
  {
    icon: '📊',
    title: 'Confidence-Scored Findings',
    desc: 'Every finding carries a 0.0–1.0 confidence score, making intelligence auditable and rankable. Certainty at scale.',
  },
  {
    icon: '💬',
    title: 'Retrieval-Grounded Answers',
    desc: 'The chat layer never hallucinates statistics. Every answer traces to a pre-computed Finding with specific evidence numbers.',
  },
  {
    icon: '⚡',
    title: 'Dual-Mode Chat',
    desc: 'RAG (insight retrieval) + NL→Pandas (direct data query) in one interface, auto-routed based on your question.',
  },
  {
    icon: '🔍',
    title: 'Deep Data Quality',
    desc: '6-dimension quality scoring across Completeness, Validity, Uniqueness, Consistency, Accuracy, and Timeliness.',
  },
  {
    icon: '💾',
    title: 'Dataset Memory',
    desc: 'Findings and embeddings persist. Return to a dataset weeks later and instantly resume your analysis conversation.',
  },
]

const COMPARISON = [
  { feature: 'Semantic Column Types', neuronova: true, ydata: false, chatgpt: false },
  { feature: 'Confidence-Scored Findings', neuronova: true, ydata: false, chatgpt: false },
  { feature: 'Hallucination-Free Answers', neuronova: true, ydata: true, chatgpt: false },
  { feature: 'Natural Language Queries', neuronova: true, ydata: false, chatgpt: true },
  { feature: 'Dataset Health Score', neuronova: true, ydata: true, chatgpt: false },
  { feature: 'Statistical Profiling', neuronova: true, ydata: true, chatgpt: false },
  { feature: 'Relationship Intelligence', neuronova: true, ydata: false, chatgpt: false },
  { feature: 'Persistent Dataset Memory', neuronova: true, ydata: false, chatgpt: false },
]

const PRICING = [
  {
    tier: 'Analyst',
    price: '$29',
    period: '/mo',
    desc: 'For individual data analysts who need fast, trustworthy EDA.',
    features: ['5 datasets / month', 'Up to 10MB per file', 'AI Insights & Chat', 'CSV & Excel support', '7-day finding history'],
    cta: 'Start Free Trial',
    featured: false,
  },
  {
    tier: 'Team',
    price: '$99',
    period: '/mo',
    desc: 'For data teams who collaborate on enterprise datasets.',
    features: ['50 datasets / month', 'Up to 100MB per file', 'All file formats', 'Shared workspace', 'Priority support', 'Dataset memory'],
    cta: 'Start Free Trial',
    featured: true,
  },
  {
    tier: 'Enterprise',
    price: 'Custom',
    period: '',
    desc: 'For organizations with large-scale data intelligence needs.',
    features: ['Unlimited datasets', 'Up to 1GB per file', 'On-premise deployment', 'SSO & audit logs', 'Dedicated support', 'Custom integrations'],
    cta: 'Contact Sales',
    featured: false,
  },
]

const SCHEMA_PREVIEW_ROWS = [
  { col: 'transaction_id', type: 'UUID', nulls: '0%', insight: 'PRIMARY KEY', insightColor: '#3525CD' },
  { col: 'customer_email', type: 'STRING', nulls: '12%', insight: 'PII DETECTED', insightColor: '#E11D48' },
  { col: 'purchase_amount', type: 'FLOAT', nulls: '0%', insight: 'FINANCIAL', insightColor: '#0D9488' },
  { col: 'region_code', type: 'CATEGORICAL', nulls: '2.1%', insight: 'GEOGRAPHIC', insightColor: '#D97706' },
]

const Check = ({ ok }) => ok ? (
  <span style={{ color: '#0D9488', fontSize: 16 }}>✓</span>
) : (
  <span style={{ color: '#D1CEC8', fontSize: 16 }}>—</span>
)

export default function Landing() {
  return (
    <div className="landing-main dot-grid">
      {/* ── Nav ── */}
      <nav className="landing-nav">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%', maxWidth: 1200, margin: '0 auto' }}>
          <Link to="/" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <img src="/logo.png" alt="NeuroNova Logo" style={{ width: 28, height: 28, borderRadius: 6 }} />
            <span style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 16, color: 'var(--color-text-primary)' }}>
              NeuroNova
            </span>
          </Link>

          <div className="desktop-menu" style={{ display: 'flex', alignItems: 'center', gap: 28 }}>
            {NAV_LINKS.map(l => (
              <a key={l} href={`#${l.toLowerCase().replace(' ', '-')}`}
                style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'var(--color-text-secondary)', transition: 'color 150ms' }}
                onMouseEnter={e => e.target.style.color = 'var(--color-text-primary)'}
                onMouseLeave={e => e.target.style.color = 'var(--color-text-secondary)'}
              >{l}</a>
            ))}
          </div>

          <Link to="/upload" className="btn btn-navy btn-lg">
            Get Early Access →
          </Link>
        </div>
      </nav>

      {/* ── Hero ── */}
      <section className="hero-section" style={{ paddingTop: 100 }}>
        <div className="anim-fade-up" style={{ animationDelay: '0ms' }}>
          <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, background: 'var(--color-primary-50)', border: '1px solid #BFDBFE', borderRadius: 'var(--radius-full)', padding: '5px 14px', marginBottom: 24 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#2563EB', display: 'inline-block' }} />
            <span style={{ fontFamily: 'var(--font-body)', fontSize: 12, fontWeight: 500, color: '#1D4ED8' }}>
              Layers 1–6 shipped · 108/108 tests passing
            </span>
          </div>

          <h1 className="text-display-lg" style={{ color: 'var(--color-text-primary)', marginBottom: 20 }}>
            Your dataset has{' '}
            <span style={{ color: 'var(--color-primary-indigo)', position: 'relative' }}>
              answers.
              <svg style={{ position: 'absolute', bottom: -4, left: 0, right: 0, width: '100%' }} height="6" viewBox="0 0 200 6" preserveAspectRatio="none">
                <path d="M0 4 Q50 1 100 3 Q150 5 200 2" stroke="var(--color-primary-indigo)" strokeWidth="2.5" fill="none" strokeLinecap="round" opacity="0.35" />
              </svg>
            </span>
            <br />NeuroNova finds them.
          </h1>

          <p className="text-body-lg" style={{ color: 'var(--color-text-secondary)', maxWidth: 560, margin: '0 auto 36px', lineHeight: 1.7 }}>
            Automated profiling, deep semantic understanding, and instant AI-driven insights for enterprise data teams. Stop wrestling with schemas and start finding value.
          </p>

          <div style={{ display: 'flex', justifyContent: 'center', gap: 12, flexWrap: 'wrap' }}>
            <Link to="/upload" className="btn btn-navy btn-lg">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="16 16 12 12 8 16" /><line x1="12" y1="12" x2="12" y2="21" />
                <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" />
              </svg>
              Upload Your First Dataset
            </Link>
            <Link to="/explorer" className="btn btn-ghost-neutral btn-lg">
              See a Live Demo
            </Link>
          </div>
        </div>

        {/* Product Preview */}
        <div className="anim-fade-up" style={{ animationDelay: '120ms', marginTop: 56 }}>
          <div className="preview-window" style={{ maxWidth: 620, margin: '0 auto', textAlign: 'left' }}>
            <div className="preview-title-bar">
              <div className="traffic-light" style={{ background: '#FF5F57' }} />
              <div className="traffic-light" style={{ background: '#FEBC2E' }} />
              <div className="traffic-light" style={{ background: '#28C840' }} />
              <span style={{ marginLeft: 12, fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-muted)' }}>Dataset Explorer · sales_data_q3.csv</span>
            </div>
            <div style={{ display: 'flex', minHeight: 220 }}>
              {/* Left panel */}
              <div style={{ width: 160, borderRight: '1px solid var(--color-border)', padding: '14px 12px', background: 'var(--color-primary)', display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'rgba(255,255,255,0.45)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>Files</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px', background: 'rgba(255,255,255,0.1)', borderRadius: 6 }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.7)" strokeWidth="2.5">
                    <rect x="3" y="3" width="18" height="18" rx="2" /><line x1="3" y1="9" x2="21" y2="9" /><line x1="9" y1="3" x2="9" y2="21" />
                  </svg>
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'rgba(255,255,255,0.8)' }}>sales_data_q3.csv</span>
                </div>
                <div style={{ marginTop: 'auto', background: 'rgba(255,255,255,0.07)', borderRadius: 8, padding: '10px 8px' }}>
                  <div style={{ fontFamily: 'var(--font-heading)', fontSize: 10, color: 'rgba(255,255,255,0.5)', marginBottom: 6 }}>Health Score</div>
                  <div style={{ fontFamily: 'var(--font-display)', fontSize: 26, fontWeight: 700, color: 'white' }}>74 <span style={{ fontSize: 12, fontFamily: 'var(--font-body)', color: 'rgba(255,255,255,0.4)' }}>/ B</span></div>
                </div>
              </div>

              {/* Schema table */}
              <div style={{ flex: 1, padding: '14px 0' }}>
                <div style={{ padding: '0 14px', marginBottom: 10 }}>
                  <span style={{ fontFamily: 'var(--font-heading)', fontSize: 13, fontWeight: 600, color: 'var(--color-text-primary)' }}>Schema Overview</span>
                </div>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                  <thead>
                    <tr style={{ background: 'var(--color-base)' }}>
                      {['Column Name', 'Type', 'Nulls', 'Insight'].map(h => (
                        <th key={h} style={{ padding: '6px 12px', fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 10, color: 'var(--color-text-muted)', textAlign: 'left', textTransform: 'uppercase', letterSpacing: '0.04em', borderBottom: '1px solid var(--color-border)' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {SCHEMA_PREVIEW_ROWS.map((row, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid var(--color-border-light)' }}>
                        <td style={{ padding: '7px 12px', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-primary)' }}>{row.col}</td>
                        <td style={{ padding: '7px 12px', fontFamily: 'var(--font-data)', fontSize: 11, color: 'var(--color-text-secondary)' }}>{row.type}</td>
                        <td style={{ padding: '7px 12px', fontFamily: 'var(--font-data)', fontSize: 11, color: row.nulls === '0%' ? 'var(--color-text-muted)' : 'var(--color-danger)' }}>{row.nulls}</td>
                        <td style={{ padding: '7px 12px' }}>
                          <span style={{ fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, color: row.insightColor, letterSpacing: '0.04em' }}>{row.insight}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ── 7-Layer Pipeline ── */}
      <section id="product" className="section" style={{ background: 'var(--color-card)', borderTop: '1px solid var(--color-border)', borderBottom: '1px solid var(--color-border)' }}>
        <div style={{ maxWidth: 1100, margin: '0 auto' }}>
          <div className="text-center mb-6">
            <p className="section-label">The Intelligence Engine</p>
            <h2 className="text-headline-md" style={{ color: 'var(--color-text-primary)', marginBottom: 8 }}>7 Layers of Data Understanding</h2>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'var(--color-text-secondary)', maxWidth: 480, margin: '0 auto' }}>
              From raw file upload to conversational analytics — a complete, sequential intelligence pipeline.
            </p>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 1, background: 'var(--color-border)', borderRadius: 12, overflow: 'hidden', marginTop: 36 }}>
            {[
              { num: '01', label: 'Upload', status: '✅', desc: 'CSV, XLSX, JSON, Parquet. Async validation.' },
              { num: '02', label: 'Profiling', status: '✅', desc: 'Schema, stats, 6 sub-profilers.' },
              { num: '03', label: 'Findings', status: '✅', desc: 'Typed, confidence-scored observations.' },
              { num: '04', label: 'Viz Metadata', status: '✅', desc: 'Chart-ready JSON payloads.' },
              { num: '05', label: 'LLM Insights', status: '✅', desc: 'gpt-4o summaries & explanations.' },
              { num: '06', label: 'Vector Index', status: '✅', desc: 'pgvector IVFFlat cosine search.' },
              { num: '07', label: 'Chat Agent', status: '⚡', desc: 'RAG + NL→Pandas, streaming.' },
            ].map((layer, i) => (
              <div key={i} style={{ background: i === 6 ? 'var(--color-primary-50)' : 'white', padding: '16px 12px', textAlign: 'center' }}>
                <div style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'var(--color-text-muted)', marginBottom: 8 }}>{layer.num}</div>
                <div style={{ fontSize: 18, marginBottom: 6 }}>{layer.status}</div>
                <div style={{ fontFamily: 'var(--font-heading)', fontSize: 12, fontWeight: 700, color: 'var(--color-text-primary)', marginBottom: 4 }}>{layer.label}</div>
                <div style={{ fontFamily: 'var(--font-body)', fontSize: 11, color: 'var(--color-text-muted)', lineHeight: 1.4 }}>{layer.desc}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Features ── */}
      <section id="features" className="section">
        <div style={{ maxWidth: 1100, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 48 }}>
            <p className="section-label">Why NeuroNova</p>
            <h2 className="text-headline-md" style={{ color: 'var(--color-text-primary)', marginBottom: 8 }}>The moat is real</h2>
            <p style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'var(--color-text-secondary)', maxWidth: 440, margin: '0 auto' }}>
              Differentiators that can't be replicated with a ChatGPT wrapper.
            </p>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 20 }}>
            {FEATURES.map((f, i) => (
              <div key={i} className="feature-card" style={{ animationDelay: `${i * 60}ms` }}>
                <div style={{ fontSize: 24, marginBottom: 12 }}>{f.icon}</div>
                <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 15, fontWeight: 600, color: 'var(--color-text-primary)', marginBottom: 8 }}>{f.title}</h3>
                <p style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)', lineHeight: 1.6 }}>{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Comparison Matrix ── */}
      <section id="use-cases" className="section" style={{ background: 'var(--color-card)', borderTop: '1px solid var(--color-border)', borderBottom: '1px solid var(--color-border)' }}>
        <div style={{ maxWidth: 900, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 40 }}>
            <p className="section-label">Comparison</p>
            <h2 className="text-headline-md" style={{ color: 'var(--color-text-primary)' }}>NeuroNova vs. the alternatives</h2>
          </div>

          <table className="comparison-table">
            <thead>
              <tr>
                <th>Feature</th>
                <th style={{ color: 'var(--color-primary-indigo)', fontFamily: 'var(--font-heading)' }}>NeuroNova</th>
                <th>ydata-profiling</th>
                <th>ChatGPT</th>
              </tr>
            </thead>
            <tbody>
              {COMPARISON.map((row, i) => (
                <tr key={i}>
                  <td style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-primary)' }}>{row.feature}</td>
                  <td style={{ textAlign: 'center' }}><Check ok={row.neuronova} /></td>
                  <td style={{ textAlign: 'center' }}><Check ok={row.ydata} /></td>
                  <td style={{ textAlign: 'center' }}><Check ok={row.chatgpt} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* ── Pricing ── */}
      <section id="pricing" className="section">
        <div style={{ maxWidth: 1000, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 48 }}>
            <p className="section-label">Pricing</p>
            <h2 className="text-headline-md" style={{ color: 'var(--color-text-primary)' }}>Simple, transparent pricing</h2>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 24 }}>
            {PRICING.map((plan, i) => (
              <div key={i} className={`pricing-card ${plan.featured ? 'featured' : ''}`}>
                {plan.featured && (
                  <div style={{ display: 'inline-block', background: 'var(--color-primary-indigo)', color: 'white', borderRadius: 'var(--radius-full)', padding: '3px 10px', fontFamily: 'var(--font-heading)', fontSize: 10, fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', marginBottom: 12 }}>
                    Most Popular
                  </div>
                )}
                <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: 16, fontWeight: 700, color: 'var(--color-text-primary)', marginBottom: 6 }}>{plan.tier}</h3>
                <p style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 16, lineHeight: 1.5 }}>{plan.desc}</p>
                <div style={{ marginBottom: 20 }}>
                  <span style={{ fontFamily: 'var(--font-display)', fontSize: 36, fontWeight: 700, color: 'var(--color-text-primary)' }}>{plan.price}</span>
                  <span style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-muted)' }}>{plan.period}</span>
                </div>
                <Link to="/upload" className={`btn w-full ${plan.featured ? 'btn-primary' : 'btn-ghost-neutral'}`} style={{ justifyContent: 'center', marginBottom: 20, display: 'flex' }}>
                  {plan.cta}
                </Link>
                <ul style={{ listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {plan.features.map((feat, j) => (
                    <li key={j} style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'var(--font-body)', fontSize: 13, color: 'var(--color-text-secondary)' }}>
                      <span style={{ color: 'var(--color-success)', fontWeight: 700, flexShrink: 0 }}>✓</span>
                      {feat}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── CTA Banner ── */}
      <section style={{ background: 'var(--color-primary)', padding: '56px 40px', textAlign: 'center' }}>
        <h2 style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 700, color: 'white', marginBottom: 12 }}>
          Your data has been waiting for this.
        </h2>
        <p style={{ fontFamily: 'var(--font-body)', fontSize: 14, color: 'rgba(255,255,255,0.65)', marginBottom: 28 }}>
          Upload your first dataset and get a full intelligence report in under 30 seconds.
        </p>
        <Link to="/upload" className="btn btn-lg" style={{ background: 'white', color: 'var(--color-primary)', fontFamily: 'var(--font-heading)', fontWeight: 700, display: 'inline-flex' }}>
          Start Finding Answers →
        </Link>
      </section>

      {/* ── Footer ── */}
      <footer className="footer">
        <div style={{ maxWidth: 1100, margin: '0 auto', display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 32 }}>
          <div>
            <div style={{ fontFamily: 'var(--font-heading)', fontWeight: 700, fontSize: 16, color: 'white', marginBottom: 6 }}>NeuroNova</div>
            <div style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'rgba(255,255,255,0.45)', marginBottom: 16 }}>Enterprise Intelligence</div>
            <div style={{ fontFamily: 'var(--font-data)', fontSize: 10, color: 'rgba(255,255,255,0.3)' }}>Built on OpenAI · pgvector · FastAPI</div>
          </div>
          <div style={{ display: 'flex', gap: 48 }}>
            {[
              { heading: 'Product', links: ['Features', 'Pricing', 'Documentation'] },
              { heading: 'Company', links: ['About', 'Blog', 'Contact'] },
            ].map(col => (
              <div key={col.heading}>
                <div style={{ fontFamily: 'var(--font-heading)', fontSize: 11, fontWeight: 700, color: 'rgba(255,255,255,0.4)', letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: 12 }}>{col.heading}</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {col.links.map(l => (
                    <a key={l} href="#" style={{ fontFamily: 'var(--font-body)', fontSize: 13, color: 'rgba(255,255,255,0.6)', transition: 'color 150ms' }}
                      onMouseEnter={e => e.target.style.color = 'white'}
                      onMouseLeave={e => e.target.style.color = 'rgba(255,255,255,0.6)'}
                    >{l}</a>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
        <div style={{ maxWidth: 1100, margin: '32px auto 0', paddingTop: 24, borderTop: '1px solid rgba(255,255,255,0.08)', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
          <span style={{ fontFamily: 'var(--font-body)', fontSize: 12, color: 'rgba(255,255,255,0.3)' }}>© 2026 NeuroNova AI. All rights reserved.</span>
          <span style={{ fontFamily: 'var(--font-data)', fontSize: 11, color: 'rgba(255,255,255,0.25)' }}>Built on OpenAI · pgvector · FastAPI</span>
        </div>
      </footer>
    </div>
  )
}
