import React, { useState, useEffect } from 'react';
import { AreaChart, Area, BarChart, Bar, LineChart, Line, ScatterChart, Scatter, XAxis, YAxis, ResponsiveContainer } from 'recharts';
import { FileText, Grid, PieChart, AlertCircle, FileSpreadsheet } from 'lucide-react';

const SCHEMA_ROWS = [
  { col: 'transaction_id', type: 'UUID', nulls: '0%', badge: 'PRIMARY KEY', color: '#2563EB', bg: '#EFF6FF' },
  { col: 'customer_email', type: 'STRING', nulls: '12%', badge: 'PII DETECTED', color: '#E11D48', bg: '#FFF1F2' },
  { col: 'purchase_amount', type: 'FLOAT', nulls: '0%', badge: 'FINANCIAL', color: '#0D9488', bg: '#F0FDFA' },
  { col: 'region_code', type: 'CATEGORICAL', nulls: '2.1%', badge: 'GEOGRAPHIC', color: '#D97706', bg: '#FFFBEB' },
  { col: 'order_date', type: 'DATETIME', nulls: '0%', badge: 'TEMPORAL', color: '#7C3AED', bg: '#F5F3FF' },
  { col: 'discount_pct', type: 'FLOAT', nulls: '4.3%', badge: 'FINANCIAL', color: '#0D9488', bg: '#F0FDFA' },
];

const FINDINGS_DATA = [
  {
    severity: 'HIGH', type: 'STRONG CORRELATION', conf: '0.97',
    color: '#E11D48', bg: '#FFF1F2', border: '#FECDD3',
    title: 'discount_pct and purchase_amount r = -0.74',
    desc: 'strong negative correlation, higher discounts are strongly associated with lower revenue per order, primary driver of Q3 AOV decline',
    evidence: 'r = -0.74\np_value = 0.0001'
  },
  {
    severity: 'MEDIUM', type: 'HIGH NULLABILITY', conf: '0.99',
    color: '#D97706', bg: '#FFFBEB', border: '#FDE68A',
    title: 'customer_email 12% null',
    desc: '98,760 rows missing email, customer attribution and cohort analysis will be incomplete for these rows',
    evidence: 'null_count = 98760\nnull_pct = 12.0\ntotal_rows = 823000'
  },
  {
    severity: 'LOW', type: 'SKEWED DISTRIBUTION', conf: '0.91',
    color: '#0D9488', bg: '#F0FDFA', border: '#99F6E4',
    title: 'purchase_amount skewness 2.34',
    desc: 'positive skew from high-value outlier orders, summary statistics mean will be misleading, prefer median for reporting',
    evidence: 'skewness = 2.34\nkurtosis = 8.1\nmean = 124.4\nmedian = 96.2'
  },
];

const DIST_DATA = [ {x:0, y:5}, {x:1, y:15}, {x:2, y:45}, {x:3, y:70}, {x:4, y:55}, {x:5, y:30}, {x:6, y:15}, {x:7, y:8}, {x:8, y:4}, {x:9, y:2} ];
const BAR_DATA = [ {name:'US-WEST', val:34}, {name:'EU-NORTH', val:28}, {name:'APAC', val:21}, {name:'OTHER', val:17} ];
const TS_DATA = [ {m:1, v:100}, {m:2, v:110}, {m:3, v:105}, {m:4, v:120}, {m:5, v:115}, {m:6, v:130}, {m:7, v:125}, {m:8, v:140}, {m:9, v:340}, {m:10, v:145}, {m:11, v:150}, {m:12, v:160} ];
const SCATTER_DATA = [ {x:10, y:90}, {x:15, y:85}, {x:20, y:70}, {x:25, y:60}, {x:30, y:65}, {x:35, y:50}, {x:40, y:45}, {x:45, y:40}, {x:50, y:25}, {x:55, y:30}, {x:60, y:15}, {x:65, y:10} ];

const STRIP_MESSAGES = {
  dist: { sev: 'LOW', color: '#0D9488', bg: '#F0FDFA', border: '#99F6E4', text: 'purchase_amount — right-skewed distribution (skewness: 2.34). Consider log-transform for ML features.' },
  bar: { sev: 'MEDIUM', color: '#D97706', bg: '#FFFBEB', border: '#FDE68A', text: 'region_code — 2.1% null values, geographic coverage incomplete for APAC.' },
  ts: { sev: 'HIGH', color: '#E11D48', bg: '#FFF1F2', border: '#FECDD3', text: 'order_date — anomalous spike detected in week of Sep 12, orders 340% above baseline, investigate source.' },
  corr: { sev: 'HIGH', color: '#E11D48', bg: '#FFF1F2', border: '#FECDD3', text: 'discount_pct and purchase_amount show r = -0.74, strong negative correlation, primary driver of AOV decline.' }
};

export default function DatasetExplorerDemo() {
  const [activeTab, setActiveTab] = useState('schema');
  const [activeChart, setActiveChart] = useState('dist');
  const [stripOpacity, setStripOpacity] = useState(1);
  const [currentStrip, setCurrentStrip] = useState(STRIP_MESSAGES.dist);

  const handleChartClick = (chartKey) => {
    if (activeChart === chartKey) return;
    setActiveChart(chartKey);
    setStripOpacity(0);
    setTimeout(() => {
      setCurrentStrip(STRIP_MESSAGES[chartKey]);
      setStripOpacity(1);
    }, 150);
  };

  return (
    <div className="preview-window" style={{ maxWidth: 900, margin: '0 auto', textAlign: 'left', backgroundColor: '#FFFFFF', border: '1px solid #E2E8F0', borderRadius: 16, overflow: 'hidden', boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.08)' }}>
      {/* Title Bar */}
      <div className="preview-title-bar" style={{ backgroundColor: '#F8FAFC', borderBottom: '1px solid #E2E8F0', padding: '12px 16px', display: 'flex', alignItems: 'center' }}>
        <div style={{ display: 'flex', gap: 8 }}>
          <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#FF5F57' }} />
          <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#FEBC2E' }} />
          <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#28C840' }} />
        </div>
        <span style={{ marginLeft: 16, fontFamily: 'JetBrains Mono, monospace', fontSize: 12, color: '#94A3B8' }}>Dataset Explorer · sales_data_q3.csv</span>
      </div>

      <div style={{ display: 'flex', height: 380 }}>
        {/* Sidebar */}
        <div style={{ width: 190, borderRight: '1px solid #E2E8F0', padding: '16px 12px 8px 12px', background: '#F8FAFC', display: 'flex', flexDirection: 'column', gap: 16, flexShrink: 0 }}>
          
          {/* FILES Section */}
          <div>
            <div style={{ fontSize: 10, fontWeight: 700, color: '#94A3B8', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }}>Files</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px', background: '#E2E8F0', borderRadius: 8 }}>
              <FileSpreadsheet size={16} color="#0F172A" />
              <div style={{ overflow: 'hidden' }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#0F172A', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>sales_data_q3.csv</div>
                <div style={{ fontSize: 11, color: '#64748B', marginTop: 2 }}>823k rows · 9.4 MB</div>
              </div>
            </div>
          </div>

          {/* VIEWS Section */}
          <div>
            <div style={{ fontSize: 10, fontWeight: 700, color: '#94A3B8', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>Views</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              {[
                { id: 'schema', label: 'Schema', icon: <Grid size={16} /> },
                { id: 'viz', label: 'Visualizations', icon: <PieChart size={16} /> },
                { id: 'findings', label: 'Findings', icon: <AlertCircle size={16} /> }
              ].map(v => (
                <button 
                  key={v.id} 
                  onClick={() => setActiveTab(v.id)}
                  onMouseEnter={(e) => { if(activeTab !== v.id) e.currentTarget.style.backgroundColor = '#F1F5F9' }}
                  onMouseLeave={(e) => { if(activeTab !== v.id) e.currentTarget.style.backgroundColor = 'transparent' }}
                  style={{ 
                    display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderRadius: 6, 
                    borderLeft: activeTab === v.id ? '3px solid #1E3A5F' : '3px solid transparent',
                    background: activeTab === v.id ? '#EFF6FF' : 'transparent',
                    color: activeTab === v.id ? '#0F172A' : '#64748B',
                    fontSize: 14, fontWeight: 500, textAlign: 'left', transition: 'background-color 150ms'
                  }}
                >
                  {React.cloneElement(v.icon, { color: activeTab === v.id ? '#1E3A5F' : '#94A3B8' })}
                  {v.label}
                </button>
              ))}
            </div>
          </div>

          {/* Health Score */}
          <div style={{ marginTop: 'auto', background: '#FFFFFF', border: '1px solid #E2E8F0', borderRadius: 12, padding: '12px' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#64748B', marginBottom: 4 }}>Health Score</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 4, marginBottom: 8 }}>
              <div style={{ fontFamily: 'Bricolage Grotesque, sans-serif', fontSize: 32, fontWeight: 700, color: '#0F172A', letterSpacing: '-0.02em', lineHeight: 1 }}>74</div>
              <div style={{ fontSize: 13, fontWeight: 500, color: '#94A3B8' }}>/100</div>
            </div>
            <div style={{ fontSize: 12, color: '#475569', marginBottom: 8 }}>Grade B — minor issues</div>
            <div style={{ height: 4, background: '#F1F5F9', borderRadius: 2, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: '74%', background: '#1E3A5F', borderRadius: 2 }}></div>
            </div>
          </div>
        </div>

        {/* Main Content Area */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', backgroundColor: '#FFFFFF', overflow: 'hidden' }}>
          {/* Header row */}
          <div style={{ padding: '20px 24px', borderBottom: '1px solid #E2E8F0', flexShrink: 0 }}>
            <h2 style={{ fontSize: 18, fontWeight: 600, color: '#0F172A', margin: 0 }}>
              {activeTab === 'schema' && 'Schema Overview'}
              {activeTab === 'viz' && 'Exploratory Visualizations'}
              {activeTab === 'findings' && 'Automated Findings'}
            </h2>
          </div>

          <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
            {/* Panel 1: SCHEMA */}
            {activeTab === 'schema' && (
              <div style={{ padding: '0 24px', overflowY: 'auto', height: '100%' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                  <thead>
                    <tr>
                      {['Column Name', 'Type', 'Nulls', 'Insight'].map(h => (
                        <th key={h} style={{ padding: '12px 0', fontWeight: 600, fontSize: 12, color: '#64748B', textAlign: 'left', textTransform: 'uppercase', borderBottom: '1px solid #E2E8F0' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {SCHEMA_ROWS.map((row, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid #F8FAFC' }}>
                        <td style={{ padding: '12px 0', fontFamily: 'JetBrains Mono, monospace', color: '#0F172A', fontSize: 12 }}>{row.col}</td>
                        <td style={{ padding: '12px 0', fontFamily: 'JetBrains Mono, monospace', color: '#64748B', fontSize: 12 }}>{row.type}</td>
                        <td style={{ padding: '12px 0', color: row.nulls === '0%' ? '#0D9488' : parseFloat(row.nulls) > 5 ? '#E11D48' : '#D97706', fontWeight: 500, fontSize: 13 }}>{row.nulls}</td>
                        <td style={{ padding: '12px 0' }}>
                          <span style={{ display: 'inline-block', fontSize: 11, fontWeight: 600, color: row.color, backgroundColor: row.bg, padding: '4px 8px', borderRadius: 9999 }}>
                            {row.badge}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Panel 2: VISUALIZATIONS */}
            {activeTab === 'viz' && (
              <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, padding: '20px 24px', flex: 1, overflowY: 'auto' }}>
                  
                  {/* Card 1: Distribution */}
                  <div 
                    onClick={() => handleChartClick('dist')}
                    onMouseEnter={(e) => { if(activeChart !== 'dist') e.currentTarget.style.borderColor = '#94A3B8' }}
                    onMouseLeave={(e) => { if(activeChart !== 'dist') e.currentTarget.style.borderColor = '#E2E8F0' }}
                    style={{ 
                      border: activeChart === 'dist' ? '1px solid #1E3A5F' : '1px solid #E2E8F0',
                      boxShadow: activeChart === 'dist' ? '0 0 0 1px #1E3A5F, 0 4px 12px rgba(30,58,95,0.08)' : 'none',
                      borderRadius: 12, padding: 16, cursor: 'pointer', transition: 'all 150ms'
                    }}
                  >
                    <div style={{ fontSize: 10, fontWeight: 700, color: '#1E3A5F', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>Distribution</div>
                    <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12, color: '#64748B', marginBottom: 12 }}>purchase_amount</div>
                    <div style={{ height: 80 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={DIST_DATA}>
                          <Area type="monotone" dataKey="y" stroke="#1E3A5F" strokeWidth={2} fill="#1E3A5F" fillOpacity={0.1} />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  </div>

                  {/* Card 2: Bar */}
                  <div 
                    onClick={() => handleChartClick('bar')}
                    onMouseEnter={(e) => { if(activeChart !== 'bar') e.currentTarget.style.borderColor = '#94A3B8' }}
                    onMouseLeave={(e) => { if(activeChart !== 'bar') e.currentTarget.style.borderColor = '#E2E8F0' }}
                    style={{ 
                      border: activeChart === 'bar' ? '1px solid #1E3A5F' : '1px solid #E2E8F0',
                      boxShadow: activeChart === 'bar' ? '0 0 0 1px #1E3A5F, 0 4px 12px rgba(30,58,95,0.08)' : 'none',
                      borderRadius: 12, padding: 16, cursor: 'pointer', transition: 'all 150ms'
                    }}
                  >
                    <div style={{ fontSize: 10, fontWeight: 700, color: '#1E3A5F', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>Bar Chart</div>
                    <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12, color: '#64748B', marginBottom: 12 }}>region_code</div>
                    <div style={{ height: 80 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={BAR_DATA} layout="vertical" margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
                          <XAxis type="number" hide />
                          <YAxis type="category" dataKey="name" hide />
                          <Bar dataKey="val" fill="#1E3A5F" radius={[0, 4, 4, 0]} />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </div>

                  {/* Card 3: Time Series */}
                  <div 
                    onClick={() => handleChartClick('ts')}
                    onMouseEnter={(e) => { if(activeChart !== 'ts') e.currentTarget.style.borderColor = '#94A3B8' }}
                    onMouseLeave={(e) => { if(activeChart !== 'ts') e.currentTarget.style.borderColor = '#E2E8F0' }}
                    style={{ 
                      border: activeChart === 'ts' ? '1px solid #1E3A5F' : '1px solid #E2E8F0',
                      boxShadow: activeChart === 'ts' ? '0 0 0 1px #1E3A5F, 0 4px 12px rgba(30,58,95,0.08)' : 'none',
                      borderRadius: 12, padding: 16, cursor: 'pointer', transition: 'all 150ms'
                    }}
                  >
                    <div style={{ fontSize: 10, fontWeight: 700, color: '#1E3A5F', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>Time Series</div>
                    <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12, color: '#64748B', marginBottom: 12 }}>order_date</div>
                    <div style={{ height: 80 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={TS_DATA}>
                          <Line type="monotone" dataKey="v" stroke="#1E3A5F" strokeWidth={2} dot={(props) => {
                            if (props.payload.m === 9) {
                              return <circle cx={props.cx} cy={props.cy} r={4} fill="#E11D48" stroke="none" />;
                            }
                            return null;
                          }} />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  </div>

                  {/* Card 4: Correlation */}
                  <div 
                    onClick={() => handleChartClick('corr')}
                    onMouseEnter={(e) => { if(activeChart !== 'corr') e.currentTarget.style.borderColor = '#94A3B8' }}
                    onMouseLeave={(e) => { if(activeChart !== 'corr') e.currentTarget.style.borderColor = '#E2E8F0' }}
                    style={{ 
                      border: activeChart === 'corr' ? '1px solid #1E3A5F' : '1px solid #E2E8F0',
                      boxShadow: activeChart === 'corr' ? '0 0 0 1px #1E3A5F, 0 4px 12px rgba(30,58,95,0.08)' : 'none',
                      borderRadius: 12, padding: 16, cursor: 'pointer', transition: 'all 150ms', position: 'relative'
                    }}
                  >
                    <div style={{ fontSize: 10, fontWeight: 700, color: '#1E3A5F', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>Correlation</div>
                    <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12, color: '#64748B', marginBottom: 12 }}>discount_pct and purchase_amount</div>
                    <div style={{ height: 80, position: 'relative' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <ScatterChart margin={{ top: 10, right: 10, bottom: 10, left: 10 }}>
                          <XAxis type="number" dataKey="x" hide domain={[0, 70]} />
                          <YAxis type="number" dataKey="y" hide domain={[0, 100]} />
                          <Scatter data={SCATTER_DATA} fill="#1E3A5F" />
                          <Line dataKey="y" stroke="#E11D48" strokeDasharray="3 3" />
                        </ScatterChart>
                      </ResponsiveContainer>
                      {/* Trend line approximation via SVG overlay */}
                      <svg style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}>
                        <line x1="10%" y1="10%" x2="90%" y2="90%" stroke="#E11D48" strokeWidth="2" strokeDasharray="4 4" />
                      </svg>
                      <div style={{ position: 'absolute', bottom: 0, right: 0, fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#64748B', background: 'rgba(255,255,255,0.8)', padding: '2px 4px', borderRadius: 4 }}>
                        r = -0.74
                      </div>
                    </div>
                  </div>

                </div>
                
                {/* Finding Strip pinned to bottom */}
                <div style={{ borderTop: '1px solid #E2E8F0', padding: '16px 24px', background: '#F8FAFC' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12, opacity: stripOpacity, transition: 'opacity 150ms' }}>
                    <span style={{ fontSize: 11, fontWeight: 700, color: currentStrip.color, backgroundColor: currentStrip.bg, border: `1px solid ${currentStrip.border}`, padding: '4px 8px', borderRadius: 6 }}>
                      {currentStrip.sev}
                    </span>
                    <span style={{ fontSize: 13, color: '#475569', lineHeight: 1.4 }}>
                      {currentStrip.text}
                    </span>
                  </div>
                </div>
              </div>
            )}

            {/* Panel 3: FINDINGS */}
            {activeTab === 'findings' && (
              <div style={{ padding: '24px', overflowY: 'auto', height: '100%', display: 'flex', flexDirection: 'column', gap: 16 }}>
                {FINDINGS_DATA.map((finding, idx) => (
                  <div key={idx} style={{ 
                    borderLeft: `3px solid ${finding.color}`, 
                    background: finding.bg, 
                    borderRadius: '0 8px 8px 0', 
                    padding: 16,
                    border: `1px solid ${finding.border}`,
                    borderLeftWidth: '3px',
                    borderLeftColor: finding.color
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                        <span style={{ fontSize: 11, fontWeight: 700, color: finding.color }}>{finding.severity}</span>
                        <span style={{ fontSize: 11, fontWeight: 600, color: '#64748B', letterSpacing: '0.05em' }}>{finding.type}</span>
                      </div>
                      <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: '#64748B' }}>
                        conf: {finding.conf}
                      </div>
                    </div>
                    <div style={{ fontSize: 14, fontWeight: 600, color: '#0F172A', marginBottom: 4 }}>
                      {finding.title}
                    </div>
                    <div style={{ fontSize: 12, color: '#475569', lineHeight: 1.5, marginBottom: 12 }}>
                      {finding.desc}
                    </div>
                    <div style={{ 
                      fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: finding.color, 
                      background: 'rgba(255,255,255,0.4)', padding: '8px 12px', borderRadius: 6, whiteSpace: 'pre-wrap' 
                    }}>
                      {finding.evidence}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
