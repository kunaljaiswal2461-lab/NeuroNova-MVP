import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { authApi } from '../api/auth';

export default function Register() {
  const { register, login } = useAuth();
  const navigate = useNavigate();
  const [formData, setFormData] = useState({ name: '', email: '', password: '' });
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setIsLoading(true);
    try {
      await register(formData.email, formData.name, formData.password);
      try {
        await login(formData.email, formData.password);
        navigate('/', { replace: true });
      } catch {
        navigate('/auth/verified', { state: { message: 'Account created. Please check your email.' }});
      }
    } catch (err) {
      setError(err.message || 'Registration failed. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleGoogle = async () => {
    setError('');
    try {
      const data = await authApi.googleStart();
      if (data.url) window.location.href = data.url;
      else setError(data.error || 'Google sign-in is not configured.');
    } catch (err) {
      setError(err.message || 'Could not start Google sign-in.');
    }
  };

  return (
    <div className="min-h-screen w-full flex flex-col items-center justify-center font-sans" style={{ backgroundColor: '#F8FAFC', padding: '48px 24px' }}>
      
      <div style={{ width: '100%', maxWidth: '440px' }}>
        
        {/* Header Section */}
        <div style={{ marginBottom: '32px', textAlign: 'center' }}>
          <h2 style={{ fontSize: '28px', fontWeight: '700', color: '#0F172A', marginBottom: '8px', letterSpacing: '-0.5px' }}>
            Create your account
          </h2>
          <p style={{ fontSize: '15px', color: '#64748B' }}>
            Start discovering AI-driven insights for your enterprise data.
          </p>
        </div>

        {/* The White Card */}
        <div style={{ 
          backgroundColor: '#FFFFFF', 
          padding: '32px', 
          borderRadius: '16px', 
          border: '1px solid #E2E8F0',
          boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03)'
        }}>
          
          {/* Google Button */}
          <button
            type="button"
            onClick={handleGoogle}
            className="flex items-center justify-center transition-opacity hover:opacity-80"
            style={{ 
              width: '100%', 
              height: '46px', 
              backgroundColor: '#FFFFFF', 
              border: '1px solid #CBD5E1', 
              borderRadius: '8px',
              gap: '12px',
              cursor: 'pointer'
            }}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
              <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
              <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
              <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
            </svg>
            <span style={{ fontSize: '14px', fontWeight: '600', color: '#334155' }}>Sign up with Google</span>
          </button>

          {/* Divider */}
          <div style={{ position: 'relative', display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '24px 0' }}>
            <div style={{ position: 'absolute', width: '100%', height: '1px', backgroundColor: '#E2E8F0' }}></div>
            <span style={{ position: 'relative', backgroundColor: '#FFFFFF', padding: '0 16px', fontSize: '13px', color: '#94A3B8' }}>
              or continue with email
            </span>
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            
            {error && (
              <div style={{ padding: '12px', backgroundColor: '#FEF2F2', border: '1px solid #FECACA', color: '#DC2626', fontSize: '13px', borderRadius: '8px', textAlign: 'center' }}>
                {error}
              </div>
            )}

            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <label style={{ fontSize: '13px', fontWeight: '500', color: '#475569' }}>Full name</label>
              <input 
                type="text" 
                required
                placeholder="Jane Doe"
                value={formData.name}
                onChange={(e) => setFormData({...formData, name: e.target.value})}
                style={{ width: '100%', height: '44px', padding: '0 14px', backgroundColor: '#FFFFFF', border: '1px solid #CBD5E1', borderRadius: '8px', fontSize: '14px', color: '#0F172A', outline: 'none' }}
              />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <label style={{ fontSize: '13px', fontWeight: '500', color: '#475569' }}>Work email</label>
              <input 
                type="email" 
                required
                placeholder="you@work-email.com"
                value={formData.email}
                onChange={(e) => setFormData({...formData, email: e.target.value})}
                style={{ width: '100%', height: '44px', padding: '0 14px', backgroundColor: '#FFFFFF', border: '1px solid #CBD5E1', borderRadius: '8px', fontSize: '14px', color: '#0F172A', outline: 'none' }}
              />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <label style={{ fontSize: '13px', fontWeight: '500', color: '#475569' }}>Password</label>
              <input 
                type="password" 
                required
                placeholder="Create a strong password"
                value={formData.password}
                onChange={(e) => setFormData({...formData, password: e.target.value})}
                style={{ width: '100%', height: '44px', padding: '0 14px', backgroundColor: '#FFFFFF', border: '1px solid #CBD5E1', borderRadius: '8px', fontSize: '14px', color: '#0F172A', outline: 'none' }}
              />
            </div>

            {/* BULLETPROOF BUTTON - IT WILL NOT DISAPPEAR THIS TIME */}
            <button 
              type="submit"
              disabled={isLoading}
              className="flex items-center justify-center transition-opacity hover:opacity-90 disabled:opacity-70 disabled:cursor-not-allowed"
              style={{ 
                width: '100%', 
                height: '46px', 
                marginTop: '8px', 
                backgroundColor: '#0F172A', 
                color: '#FFFFFF', 
                fontSize: '14px', 
                fontWeight: '600', 
                borderRadius: '8px',
                border: 'none',
                gap: '8px',
                cursor: isLoading ? 'not-allowed' : 'pointer'
              }}
            >
              {isLoading ? 'Creating account...' : 'Get Early Access'}
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#FFFFFF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.8 }}>
                <path d="M5 12h14M12 5l7 7-7 7"/>
              </svg>
            </button>
          </form>

        </div>

        {/* Footer */}
        <div style={{ marginTop: '32px', textAlign: 'center' }}>
          <span style={{ fontSize: '14px', color: '#64748B' }}>Already have an account? </span>
          <Link to="/login" style={{ fontSize: '14px', fontWeight: '600', color: '#0F172A', textDecoration: 'none' }}>
            Log in
          </Link>
        </div>

      </div>
    </div>
  );
}