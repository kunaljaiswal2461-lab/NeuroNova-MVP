import { useEffect, useState } from 'react';
import { useNavigate, Link, useLocation, useSearchParams } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { authApi } from '../api/auth';

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const [formData, setFormData] = useState({ email: '', password: '' });
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    const oauthError = searchParams.get('error');
    if (oauthError) setError(decodeURIComponent(oauthError));
  }, [searchParams]);

  // Where to go after logging in (default to upload page, not marketing landing)
  const from = location.state?.from?.pathname || '/upload';

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setIsLoading(true);
    try {
      await login(formData.email, formData.password);
      navigate(from, { replace: true });
    } catch (err) {
      if (err.message?.includes('not verified')) {
        setError('Your email is not verified. Please check your inbox.');
      } else {
        setError(err.message || 'Login failed. Please check your credentials.');
      }
    } finally {
      setIsLoading(false);
    }
  };

  const handleGoogle = async () => {
    setError('');
    try {
      const data = await authApi.googleStart();
      if (data.url) {
        window.location.href = data.url;
      } else {
        setError(data.error || 'Google sign-in is not configured on this server.');
      }
    } catch (err) {
      setError(err.message || 'Could not start Google sign-in.');
    }
  };

  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-[#F8F9FA] relative py-[64px]">
      {/* Background Grid */}
      <div 
        className="absolute inset-0 pointer-events-none"
        style={{
          backgroundSize: '24px 24px',
          backgroundImage: 'radial-gradient(circle, #E5E7EB 1px, transparent 1px)'
        }}
      />

      <div className="relative z-10 w-full max-w-[480px] px-6">
        
        {/* Header */}
        <div className="text-center mb-[48px]">
          <h1 className="text-[40px] leading-[48px] font-bold text-[#111827] tracking-[-0.02em] mb-2 font-['Inter',sans-serif]">
            Log in to NeuroNova
          </h1>
          <p className="text-[18px] leading-[28px] font-normal text-[#4B5563] font-['Inter',sans-serif]">
            Welcome back to your enterprise insights.
          </p>
        </div>

        {/* Card Wrapper with Brackets */}
        <div className="relative">
          {/* Top Left Bracket */}
          <div className="absolute -top-1 -left-1 w-6 h-6 border-t-[4px] border-l-[4px] border-[#6B7280]" />
          {/* Top Right Bracket */}
          <div className="absolute -top-1 -right-1 w-6 h-6 border-t-[4px] border-r-[4px] border-[#6B7280]" />
          {/* Bottom Left Bracket */}
          <div className="absolute -bottom-1 -left-1 w-6 h-6 border-b-[4px] border-l-[4px] border-[#6B7280]" />
          {/* Bottom Right Bracket */}
          <div className="absolute -bottom-1 -right-1 w-6 h-6 border-b-[4px] border-r-[4px] border-[#6B7280]" />

          {/* Form Card */}
          <div className="bg-[#F9FAFB] p-[32px] shadow-sm rounded-none border border-transparent relative z-10">
            
            {/* OAuth Buttons */}
            <div className="flex flex-row gap-[16px] mb-6">
              <button
                type="button"
                onClick={handleGoogle}
                className="flex-1 flex items-center justify-center gap-2 h-11 bg-white border border-[#D1D5DB] rounded-[6px] hover:bg-[#F3F4F6] transition-colors"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
                  <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
                  <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
                  <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
                </svg>
                <span className="text-[14px] font-semibold text-[#111827]">Continue with Google</span>
              </button>

              <button
                type="button"
                onClick={() => setError('Microsoft sign-in is coming soon.')}
                title="Coming soon"
                className="flex-1 flex items-center justify-center gap-2 h-11 bg-white border border-[#D1D5DB] rounded-[6px] hover:bg-[#F3F4F6] transition-colors opacity-60 cursor-not-allowed"
              >
                <svg width="18" height="18" viewBox="0 0 21 21" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M10 0H0V10H10V0Z" fill="#F25022"/>
                  <path d="M21 0H11V10H21V0Z" fill="#7FBA00"/>
                  <path d="M10 11H0V21H10V11Z" fill="#00A4EF"/>
                  <path d="M21 11H11V21H21V11Z" fill="#FFB900"/>
                </svg>
                <span className="text-[14px] font-semibold text-[#111827]">Continue with Microsoft</span>
              </button>
            </div>

            {/* Divider */}
            <div className="flex items-center my-[24px]">
              <div className="flex-1 h-[1px] bg-[#D1D5DB]"></div>
              <span className="px-4 text-[14px] text-[#6B7280]">or continue with email</span>
              <div className="flex-1 h-[1px] bg-[#D1D5DB]"></div>
            </div>

            {/* Form */}
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              
              {error && (
                <div className="p-3 bg-red-50 border border-red-200 text-red-700 text-sm rounded-[6px]">
                  {error}
                </div>
              )}

              <div className="flex flex-col gap-1">
                <label className="text-[14px] font-medium text-[#4B5563]">Work email</label>
                <input 
                  type="email" 
                  required
                  placeholder="you@work-email.com"
                  value={formData.email}
                  onChange={(e) => setFormData({...formData, email: e.target.value})}
                  className="h-[44px] px-3 bg-[#E5E5E5] border border-[#9CA3AF] rounded-[6px] text-[#111827] focus:outline-none focus:ring-2 focus:ring-[#1E3A5F] focus:border-transparent placeholder-[#4B5563] transition-all"
                />
              </div>

              <div className="flex flex-col gap-1">
                <div className="flex items-center justify-between">
                  <label className="text-[14px] font-medium text-[#4B5563]">Password</label>
                  <Link to="/auth/forgot" className="text-[12px] text-[#1E3A5F] hover:underline">
                    Forgot password?
                  </Link>
                </div>
                <input 
                  type="password" 
                  required
                  placeholder="••••••••••••"
                  value={formData.password}
                  onChange={(e) => setFormData({...formData, password: e.target.value})}
                  className="h-[44px] px-3 bg-[#E5E5E5] border border-[#9CA3AF] rounded-[6px] text-[#111827] focus:outline-none focus:ring-2 focus:ring-[#1E3A5F] focus:border-transparent placeholder-[#4B5563] transition-all"
                />
              </div>

              <button 
                type="submit"
                disabled={isLoading}
                className="relative w-full h-[48px] mt-2 bg-[#1E3A5F] hover:bg-[#152A45] text-white font-bold text-[16px] rounded-[6px] transition-all hover:scale-[1.01] flex items-center justify-center gap-2 disabled:opacity-70 disabled:hover:scale-100"
              >
                {isLoading ? 'Logging in...' : 'Log in →'}
                {/* Sparkle Icon */}
                <svg className="absolute bottom-[4px] right-[8px] opacity-100" width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M12 2L14.4 9.6L22 12L14.4 14.4L12 22L9.6 14.4L2 12L9.6 9.6L12 2Z" fill="#FFFFFF"/>
                </svg>
              </button>
            </form>

          </div>
        </div>

        {/* Footer */}
        <div className="mt-8 text-center">
          <span className="text-[14px] text-[#4B5563]">New user? </span>
          <Link to="/register" className="text-[14px] text-[#4B5563] underline hover:text-[#1E3A5F] transition-colors">
            Sign up
          </Link>
        </div>

      </div>
    </div>
  );
}
