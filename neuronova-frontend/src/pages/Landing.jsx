import { Link, Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

const NAV_LINKS = ['Product', 'How It Works', 'Use Cases', 'Pricing']

// Replaced emojis with crisp SVG icons (using Lucide/Heroicons style)
const FEATURES = [
  {
    icon: <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>,
    title: 'Semantic Column Intelligence',
    desc: 'NeuroNova identifies what a column represents — FINANCIAL, GEOGRAPHIC, EMAIL — not just its data type. Generic tools can\'t do this.',
  },
  {
    icon: <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>,
    title: 'Confidence-Scored Findings',
    desc: 'Every finding carries a 0.0–1.0 confidence score, making intelligence auditable and rankable. Certainty at scale.',
  },
  {
    icon: <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>,
    title: 'Retrieval-Grounded Answers',
    desc: 'The chat layer never hallucinates statistics. Every answer traces to a pre-computed Finding with specific evidence numbers.',
  },
  {
    icon: <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>,
    title: 'Dual-Mode Chat',
    desc: 'RAG (insight retrieval) + NL→Pandas (direct data query) in one interface, auto-routed based on your question.',
  },
  {
    icon: <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><polyline points="11 8 11 11 14 14"/></svg>,
    title: 'Deep Data Quality',
    desc: '6-dimension quality scoring across Completeness, Validity, Uniqueness, Consistency, Accuracy, and Timeliness.',
  },
  {
    icon: <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3B82F6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12H2"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/><line x1="10" y1="16" x2="14" y2="16"/></svg>,
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
  { col: 'transaction_id', type: 'UUID', nulls: '0%', insight: 'PRIMARY KEY', insightColor: '#3B82F6' },
  { col: 'customer_email', type: 'STRING', nulls: '12%', insight: 'PII DETECTED', insightColor: '#EF4444' },
  { col: 'purchase_amount', type: 'FLOAT', nulls: '0%', insight: 'FINANCIAL', insightColor: '#10B981' },
  { col: 'region_code', type: 'CATEGORICAL', nulls: '2.1%', insight: 'GEOGRAPHIC', insightColor: '#F59E0B' },
]

const Check = ({ ok }) => ok ? (
  <span style={{ color: '#10B981', fontSize: 18, fontWeight: 'normal' }}>✓</span>
) : (
  <span style={{ color: '#CBD5E1', fontSize: 18 }}>—</span>
)

export default function Landing() {
  const { user, loading } = useAuth();
  const ctaLink = user ? '/upload' : '/register';

  if (loading) return null;
  // Removed forced redirect so logged-in users can still view the landing page
  // (e.g. via the "Explore" button or logo click from the dashboard)
  // if (user) return <Navigate to="/upload" replace />;

  return (
    <div className="landing-main dot-grid" style={{ backgroundColor: '#FFFFFF', color: '#0F172A', minHeight: '100vh', fontFamily: '"Inter", system-ui, sans-serif' }}>
      
      {/* Injecting CSS for the floating micro-animation */}
      <style>
        {`
          @keyframes float {
            0% { transform: translateY(0px); }
            50% { transform: translateY(-12px); }
            100% { transform: translateY(0px); }
          }
          .floating-ui {
            animation: float 6s ease-in-out infinite;
          }
        `}
      </style>

      {/* ── Nav ── */}
      <nav className="landing-nav" style={{ padding: 0, backgroundColor: 'rgba(255, 255, 255, 0.9)', borderBottom: '1px solid #E2E8F0', position: 'sticky', top: 0, zIndex: 50, backdropFilter: 'blur(8px)' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%', maxWidth: 1200, margin: '0 auto', paddingTop: '8px', paddingBottom: '8px', paddingLeft: '24px', paddingRight: '24px', position: 'relative' }}>
          
          <Link to="/" style={{ display: 'flex', alignItems: 'center', gap: 8, textDecoration: 'none' }}>
            <img src="/logo.png" alt="NeuroNova Logo" style={{ width: 28, height: 28, borderRadius: 6 }} />
            <span style={{ fontWeight: 700, fontSize: 18, color: '#0F172A', letterSpacing: '-0.02em' }}>
              NeuroNova
            </span>
          </Link>

          <div style={{ display: 'flex', alignItems: 'center', gap: 32, position: 'absolute', left: '50%', transform: 'translateX(-50%)' }}>
            {NAV_LINKS.map(l => (
              <a key={l} href={`#${l.toLowerCase().replace(' ', '-')}`}
                style={{ fontSize: 14, fontWeight: 500, color: '#64748B', textDecoration: 'none', transition: 'color 150ms' }}
                onMouseEnter={e => e.target.style.color = '#0F172A'}
                onMouseLeave={e => e.target.style.color = '#64748B'}
              >{l}</a>
            ))}
          </div>

          <Link to={ctaLink} style={{ backgroundColor: '#0F172A', color: '#FFFFFF', padding: '10px 20px', borderRadius: '8px', fontSize: 14, fontWeight: 600, textDecoration: 'none' }}>
            {user ? 'Go to Dashboard →' : 'Get Early Access →'}
          </Link>
          
        </div>
      </nav>

      {/* ── Hero ── */}
      <section className="hero-section" style={{ textAlign: 'center', padding: '120px 24px 60px' }}>
        <div className="anim-fade-up" style={{ animationDelay: '0ms', maxWidth: 1100, margin: '0 auto' }}>
          
          <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, background: '#EFF6FF', border: '1px solid #BFDBFE', borderRadius: '999px', padding: '6px 16px', marginBottom: 24 }}>

            <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#3B82F6', display: 'inline-block' }} />
            <span style={{ fontSize: 13, fontWeight: 600, color: '#1D4ED8' }}>
              Layers 1–6 shipped · 108/108 tests passing
            </span>
          </div>

          {/* Adjusted clamp size to (36px, 5vw, 64px) so it stays on one line */}
          <h1 style={{ fontSize: 'clamp(36px, 5vw, 64px)', color: '#0F172A', marginBottom: 20, fontWeight: 800, lineHeight: 1.1, letterSpacing: '-0.04em' }}>
            Your dataset has{' '}
            <span style={{ color: '#3B82F6', position: 'relative', whiteSpace: 'nowrap' }}>
              answers.
              <svg style={{ position: 'absolute', bottom: -4, left: 0, right: 0, width: '100%' }} height="8" viewBox="0 0 200 8" preserveAspectRatio="none">
                <path d="M0 6 Q50 2 100 5 Q150 7 200 3" stroke="#BFDBFE" strokeWidth="4" fill="none" strokeLinecap="round" opacity="0.8" />
              </svg>
            </span>
            <br />NeuroNova finds them.
          </h1>

          <p style={{ fontSize: 18, color: '#64748B', maxWidth: 640, margin: '0 auto 36px', lineHeight: 1.6 }}>
            Automated profiling, deep semantic understanding, and instant AI-driven insights for enterprise data teams. Stop wrestling with schemas and start finding value.
          </p>

          <div style={{ display: 'flex', justifyContent: 'center', gap: 16, flexWrap: 'wrap' }}>
            <Link to={ctaLink} style={{ backgroundColor: '#0F172A', color: '#FFFFFF', padding: '14px 28px', borderRadius: '10px', fontSize: 16, fontWeight: 600, textDecoration: 'none' }}>
              {user ? 'Go to Dashboard' : 'Upload Your First Dataset'}
            </Link>
            <Link to="/explorer" style={{ backgroundColor: '#FFFFFF', border: '1px solid #E2E8F0', color: '#0F172A', padding: '14px 28px', borderRadius: '10px', fontSize: 16, fontWeight: 600, textDecoration: 'none' }}>
              See a Live Demo
            </Link>
          </div>
        </div>

        {/* Product Preview with floating animation */}
        <div className="anim-fade-up floating-ui" style={{ animationDelay: '120ms', marginTop: 80 }}>
          <div className="preview-window" style={{ maxWidth: 800, margin: '0 auto', textAlign: 'left', backgroundColor: '#FFFFFF', border: '1px solid #E2E8F0', borderRadius: 16, overflow: 'hidden', boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.08)' }}>
            <div className="preview-title-bar" style={{ backgroundColor: '#F8FAFC', borderBottom: '1px solid #E2E8F0', padding: '12px 16px', display: 'flex', alignItems: 'center' }}>
              <div style={{ display: 'flex', gap: 8 }}>
                <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#FF5F57' }} />
                <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#FEBC2E' }} />
                <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#28C840' }} />
              </div>
              <span style={{ marginLeft: 16, fontFamily: 'monospace', fontSize: 12, color: '#94A3B8' }}>Dataset Explorer · sales_data_q3.csv</span>
            </div>
            <div style={{ display: 'flex', minHeight: 300 }}>
              <div style={{ width: 200, borderRight: '1px solid #E2E8F0', padding: '20px', background: '#F8FAFC', display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: '#94A3B8', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Files</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px', background: '#E2E8F0', borderRadius: 8 }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: '#0F172A' }}>sales_data_q3.csv</span>
                </div>
                <div style={{ marginTop: 'auto', background: '#FFFFFF', border: '1px solid #E2E8F0', borderRadius: 12, padding: '16px' }}>
                  <div style={{ fontSize: 11, fontWeight: 600, color: '#64748B', marginBottom: 4 }}>Health Score</div>
                  <div style={{ fontSize: 32, fontWeight: 800, color: '#0F172A', letterSpacing: '-0.02em' }}>74 <span style={{ fontSize: 14, fontWeight: 500, color: '#94A3B8' }}>/ 100</span></div>
                </div>
              </div>

              <div style={{ flex: 1, padding: '24px' }}>
                <div style={{ marginBottom: 20 }}>
                  <span style={{ fontSize: 16, fontWeight: 700, color: '#0F172A', letterSpacing: '-0.02em' }}>Schema Overview</span>
                </div>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                  <thead>
                    <tr>
                      {['Column Name', 'Type', 'Nulls', 'Insight'].map(h => (
                        <th key={h} style={{ padding: '10px 12px', fontWeight: 600, fontSize: 12, color: '#64748B', textAlign: 'left', textTransform: 'uppercase', borderBottom: '1px solid #E2E8F0' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {SCHEMA_PREVIEW_ROWS.map((row, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid #F1F5F9' }}>
                        <td style={{ padding: '14px 12px', fontWeight: 500, color: '#0F172A' }}>{row.col}</td>
                        <td style={{ padding: '14px 12px', fontFamily: 'monospace', color: '#64748B' }}>{row.type}</td>
                        <td style={{ padding: '14px 12px', color: row.nulls === '0%' ? '#94A3B8' : '#EF4444', fontWeight: row.nulls === '0%' ? 400 : 600 }}>{row.nulls}</td>
                        <td style={{ padding: '14px 12px' }}>
                          <span style={{ fontSize: 11, fontWeight: 700, color: row.insightColor, backgroundColor: `${row.insightColor}15`, padding: '6px 10px', borderRadius: 6 }}>
                            {row.insight}
                          </span>
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

      {/* ── Integration Banner ── */}
      <div style={{ borderTop: '1px solid #E2E8F0', borderBottom: '1px solid #E2E8F0', padding: '32px 24px', backgroundColor: '#F8FAFC', display: 'flex', justifyContent: 'center', gap: 'clamp(24px, 5vw, 64px)', flexWrap: 'wrap', alignItems: 'center' }}>
        {['Powered by', 'LangChain', 'LangGraph', 'OpenAI', 'pgvector', 'Python'].map((tech, i) => (
          <span key={tech} style={{ 
            fontSize: i === 0 ? 12 : 16, 
            fontWeight: i === 0 ? 600 : 700, 
            color: i === 0 ? '#94A3B8' : '#cbd5e1', 
            textTransform: i === 0 ? 'uppercase' : 'none', 
            letterSpacing: i === 0 ? '1px' : '-0.02em',
            filter: i !== 0 ? 'grayscale(100%)' : 'none'
          }}>
            {tech}
          </span>
        ))}
      </div>

      {/* ── 7-Layer Pipeline ── */}
      <section id="product" style={{ background: '#FFFFFF', borderBottom: '1px solid #E2E8F0', padding: '100px 24px' }}>
        <div style={{ maxWidth: 1100, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 48 }}>
            <p style={{ color: '#3B82F6', fontWeight: 700, fontSize: 12, textTransform: 'uppercase', letterSpacing: '1px', marginBottom: 8 }}>The Intelligence Engine</p>
            <h2 style={{ color: '#0F172A', fontSize: 32, fontWeight: 800, marginBottom: 12, letterSpacing: '-0.03em' }}>7 Layers of Data Understanding</h2>
            <p style={{ fontSize: 16, color: '#64748B', maxWidth: 500, margin: '0 auto' }}>
              From raw file upload to conversational analytics — a complete, sequential intelligence pipeline.
            </p>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 1, background: '#E2E8F0', borderRadius: 12, overflow: 'hidden' }}>
            {[
              { num: '01', label: 'Upload', status: '✅', desc: 'CSV, XLSX, JSON, Parquet. Async validation.' },
              { num: '02', label: 'Profiling', status: '✅', desc: 'Schema, stats, 6 sub-profilers.' },
              { num: '03', label: 'Findings', status: '✅', desc: 'Typed, confidence-scored observations.' },
              { num: '04', label: 'Viz Metadata', status: '✅', desc: 'Chart-ready JSON payloads.' },
              { num: '05', label: 'LLM Insights', status: '✅', desc: 'gpt-4 summaries & explanations.' },
              { num: '06', label: 'Vector Index', status: '✅', desc: 'pgvector IVFFlat cosine search.' },
              { num: '07', label: 'Chat Agent', status: '⚡', desc: 'RAG + NL→Pandas, streaming.' },
            ].map((layer, i) => (
              <div key={i} style={{ background: i === 6 ? '#EFF6FF' : '#FFFFFF', padding: '24px 16px', textAlign: 'center' }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: '#94A3B8', marginBottom: 12 }}>{layer.num}</div>
                <div style={{ fontSize: 20, marginBottom: 8 }}>{layer.status}</div>
                <div style={{ fontSize: 14, fontWeight: 700, color: '#0F172A', marginBottom: 8, letterSpacing: '-0.02em' }}>{layer.label}</div>
                <div style={{ fontSize: 12, color: '#64748B', lineHeight: 1.5 }}>{layer.desc}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Features (The Moat) ── */}
      <section id="how-it-works" style={{ padding: '100px 24px' }}>
        <div style={{ maxWidth: 1100, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 64 }}>
            <p style={{ fontSize: 12, fontWeight: 700, color: '#3B82F6', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: 8 }}>Why NeuroNova</p>
            <h2 style={{ fontSize: 32, fontWeight: 800, color: '#0F172A', marginBottom: 16, letterSpacing: '-0.03em' }}>The moat is real</h2>
            <p style={{ fontSize: 16, color: '#64748B', maxWidth: 500, margin: '0 auto' }}>
              Differentiators that can't be replicated with a ChatGPT wrapper.
            </p>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 24 }}>
            {FEATURES.map((f, i) => (
              <div key={i} style={{ backgroundColor: '#FFFFFF', padding: '32px', borderRadius: 16, border: '1px solid #F1F5F9', boxShadow: '0 10px 30px -10px rgba(0, 0, 0, 0.05)', transition: 'transform 200ms ease, box-shadow 200ms ease' }}
                   onMouseEnter={e => { e.currentTarget.style.transform = 'translateY(-4px)'; e.currentTarget.style.boxShadow = '0 20px 40px -10px rgba(0, 0, 0, 0.08)'; }}
                   onMouseLeave={e => { e.currentTarget.style.transform = 'translateY(0)'; e.currentTarget.style.boxShadow = '0 10px 30px -10px rgba(0, 0, 0, 0.05)'; }}
              >
                <div style={{ marginBottom: 20 }}>{f.icon}</div>
                <h3 style={{ fontSize: 18, fontWeight: 700, color: '#0F172A', marginBottom: 12, letterSpacing: '-0.02em' }}>{f.title}</h3>
                <p style={{ fontSize: 15, color: '#64748B', lineHeight: 1.6 }}>{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Comparison Matrix ── */}
      <section id="use-cases" style={{ padding: '40px 24px 100px' }}>
        <div style={{ maxWidth: 900, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 48 }}>
            <p style={{ fontSize: 12, fontWeight: 700, color: '#3B82F6', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: 8 }}>Comparison</p>
            <h2 style={{ fontSize: 32, fontWeight: 800, color: '#0F172A', letterSpacing: '-0.03em' }}>NeuroNova vs. the alternatives</h2>
          </div>

          <div style={{ backgroundColor: '#FFFFFF', borderRadius: 16, border: '1px solid #E2E8F0', boxShadow: '0 10px 30px -10px rgba(0, 0, 0, 0.05)', overflow: 'hidden' }}>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'center', minWidth: 600 }}>
                <thead style={{ backgroundColor: '#F8FAFC' }}>
                  <tr>
                    <th style={{ textAlign: 'left', padding: '20px 24px', borderBottom: '1px solid #E2E8F0', fontSize: 13, color: '#64748B', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Feature</th>
                    <th style={{ padding: '20px 24px', borderBottom: '1px solid #E2E8F0', fontSize: 13, color: '#3B82F6', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '0.5px' }}>NeuroNova</th>
                    <th style={{ padding: '20px 24px', borderBottom: '1px solid #E2E8F0', fontSize: 13, color: '#64748B', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.5px' }}>ydata-profiling</th>
                    <th style={{ padding: '20px 24px', borderBottom: '1px solid #E2E8F0', fontSize: 13, color: '#64748B', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.5px' }}>ChatGPT</th>
                  </tr>
                </thead>
                <tbody>
                  {COMPARISON.map((row, i) => (
                    <tr key={i} style={{ borderBottom: i === COMPARISON.length - 1 ? 'none' : '1px solid #F1F5F9' }}>
                      <td style={{ textAlign: 'left', padding: '20px 24px', fontSize: 15, fontWeight: 500, color: '#0F172A' }}>{row.feature}</td>
                      <td style={{ padding: '20px 24px', backgroundColor: '#F8FAFC' }}><Check ok={row.neuronova} /></td>
                      <td style={{ padding: '20px 24px' }}><Check ok={row.ydata} /></td>
                      <td style={{ padding: '20px 24px' }}><Check ok={row.chatgpt} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </section>

      {/* ── Founders / Trust Section ── */}
      <section style={{ padding: '100px 24px', backgroundColor: '#F8FAFC', borderTop: '1px solid #E2E8F0' }}>
        <div style={{ maxWidth: 800, margin: '0 auto', textAlign: 'center' }}>
          <p style={{ fontSize: 12, fontWeight: 700, color: '#3B82F6', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: 8 }}>The Team</p>
          <h2 style={{ fontSize: 32, fontWeight: 800, color: '#0F172A', marginBottom: 24, letterSpacing: '-0.03em' }}>Built by AI Specialists</h2>
          <p style={{ fontSize: 18, color: '#64748B', lineHeight: 1.6, marginBottom: 32 }}>
                  NeuroNova isn't just another dashboard — it's an AI Analyst that reads, understands, 
                  and explains your data the way a human analyst would, minus the wait. 
                  Built by engineers who believe data intelligence should be automatic, not manual. 
          </p>
        </div>
      </section>

      {/* ── Pricing ── */}
      <section id="pricing" style={{ backgroundColor: '#FFFFFF', padding: '100px 24px', borderTop: '1px solid #E2E8F0' }}>
        <div style={{ maxWidth: 1000, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 64 }}>
            <h2 style={{ fontSize: 32, fontWeight: 800, color: '#0F172A', letterSpacing: '-0.03em' }}>Simple, transparent pricing</h2>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 32 }}>
            {PRICING.map((plan, i) => (
              <div key={i} style={{ 
                backgroundColor: plan.featured ? '#0F172A' : '#FFFFFF',
                border: plan.featured ? 'none' : '1px solid #E2E8F0',
                padding: '48px 32px',
                borderRadius: 24,
                position: 'relative',
                boxShadow: plan.featured ? '0 20px 40px -10px rgba(0,0,0,0.2)' : '0 10px 30px -10px rgba(0,0,0,0.05)',
                transition: 'transform 200ms ease, box-shadow 200ms ease'
              }}
                onMouseEnter={e => {
                  e.currentTarget.style.transform= 'translateY(-6px)';
                  e.currentTarget.style.boxShadow= plan.featured
                    ? '0 30px 50px -10px rgba(0,0,0,0.3)'
                    : '0 20px 40px -10px rgba(0,0,0,0.08)';
                }}
                onMouseLeave={e => {
                  e.currentTarget.style.transform='translateY(0)';
                  e.currentTarget.style.boxShadow=plan.featured
                    ? '0 20px 40px -10px rgba(0,0,0,0.2)'
                    : '0 10px 30px -10px rgba(0,0,0,0.05)';
                }}
              >
                {plan.featured && (
                  <div style={{ position: 'absolute', top: -12, left: '50%', transform: 'translateX(-50%)', background: '#3B82F6', color: '#FFFFFF', borderRadius: '999px', padding: '6px 16px', fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                    Most Popular
                  </div>
                )}
                <h3 style={{ fontSize: 24, fontWeight: 700, color: plan.featured ? '#FFFFFF' : '#0F172A', marginBottom: 12, letterSpacing: '-0.02em' }}>{plan.tier}</h3>
                <p style={{ fontSize: 15, color: plan.featured ? '#94A3B8' : '#64748B', marginBottom: 32, minHeight: 48, lineHeight: 1.5 }}>{plan.desc}</p>
                <div style={{ marginBottom: 32 }}>
                  <span style={{ fontSize: 48, fontWeight: 800, color: plan.featured ? '#FFFFFF' : '#0F172A', letterSpacing: '-0.04em' }}>{plan.price}</span>
                  <span style={{ fontSize: 16, color: plan.featured ? '#94A3B8' : '#64748B', marginLeft: 6 }}>{plan.period}</span>
                </div>
                <Link to="/register" style={{ 
                  justifyContent: 'center', 
                  marginBottom: 40, 
                  display: 'flex',
                  padding: '16px',
                  borderRadius: '12px',
                  fontWeight: 600,
                  textDecoration: 'none',
                  backgroundColor: plan.featured ? '#3B82F6' : '#F8FAFC',
                  color: plan.featured ? '#FFFFFF' : '#0F172A',
                  border: plan.featured ? 'none' : '1px solid #E2E8F0'
                }}>
                  {plan.cta}
                </Link>
                <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 16 }}>
                  {plan.features.map((feat, j) => (
                    <li key={j} style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 15, color: plan.featured ? '#CBD5E1' : '#475569' }}>
                      <span style={{ color: '#3B82F6', fontWeight: 700 }}>✓</span>
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
      <section style={{ background: '#0F172A', padding: '100px 24px', textAlign: 'center' }}>
        <h2 style={{ fontSize: 36, fontWeight: 800, color: '#FFFFFF', marginBottom: 20, letterSpacing: '-0.03em' }}>
          Your data has been waiting for this.
        </h2>
        <p style={{ fontSize: 18, color: '#94A3B8', marginBottom: 40, maxWidth: 600, margin: '0 auto 40px', lineHeight: 1.6 }}>
          Upload your first dataset and get a full intelligence report in under 30 seconds.
        </p>
        <Link to={ctaLink} style={{ background: '#FFFFFF', color: '#0F172A', padding: '16px 32px', borderRadius: '12px', fontSize: 16, fontWeight: 700, textDecoration: 'none', display: 'inline-flex' }}>
          {user ? 'Go to Dashboard →' : 'Start Finding Answers →'}
        </Link>
      </section>

      {/* ── Footer ── */}
      <footer style={{ backgroundColor: '#0F172A', borderTop: '1px solid #1E293B', padding: '80px 24px 40px' }}>
        <div style={{ maxWidth: 1100, margin: '0 auto', display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 40 }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
              <img src="/logo.png" alt="NeuroNova Logo" style={{ width: 28, height: 28, borderRadius: 6 }} />
              <span style={{ fontWeight: 700, fontSize: 18, color: '#FFFFFF', letterSpacing: '-0.02em' }}>NeuroNova</span>
            </div> 
            <div style={{ fontSize: 14, color: '#94A3B8' }}>Data intelligence, automated and secure.</div>
          </div>
          <div style={{ display: 'flex', gap: 80 }}>
            {[
              { heading: 'Product', links: ['Features', 'Pricing', 'Documentation'] },
              { heading: 'Company', links: ['About', 'Blog', 'Contact'] },
            ].map(col => (
              <div key={col.heading}>
                <div style={{ fontSize: 13, fontWeight: 700, color: '#FFFFFF', textTransform: 'uppercase', letterSpacing: '1px', marginBottom: 20 }}>{col.heading}</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                  {col.links.map(l => (
                    <a key={l} href="#" style={{ fontSize: 15, color: '#94A3B8', textDecoration: 'none' }}>{l}</a>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
        <div style={{ maxWidth: 1100, margin: '64px auto 0', paddingTop: 32, borderTop: '1px solid #1E293B', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 16 }}>
          <span style={{ fontSize: 14, color: '#64748B' }}>© {new Date().getFullYear()} NeuroNova AI. All rights reserved.</span>
          <span style={{ fontFamily: 'monospace', fontSize: 13, color: '#475569' }}>Built on pgvector · FastAPI</span>
        </div>
      </footer>
    </div>
  )
}