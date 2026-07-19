import { useState, useEffect } from 'react';
import { useNavigate, useSearchParams, Link } from 'react-router-dom';
import { authApi } from '../api/auth';

export default function ResetPassword() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const token = searchParams.get('token');

  useEffect(() => {
    if (!token) {
      setError('Invalid reset link. Please request a new one.');
    }
  }, [token]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    if (password !== confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    if (password.length < 8) {
      setError('Password must be at least 8 characters.');
      return;
    }

    setIsLoading(true);
    try {
      await authApi.resetPassword(token, password);
      navigate('/login', { replace: true });
    } catch (err) {
      setError(err.message || 'Failed to reset password. The link may have expired.');
    } finally {
      setIsLoading(false);
    }
  };

  if (!token) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-[#F8F9FA] px-6">
        <div className="max-w-md text-center">
          <h1 className="text-2xl font-bold text-[#111827] mb-2">Invalid link</h1>
          <p className="text-[#4B5563] mb-6">{error}</p>
          <Link
            to="/auth/forgot"
            className="inline-block px-5 h-11 bg-[#1E3A5F] hover:bg-[#152A45] text-white font-semibold rounded-[6px]"
          >
            Request new link
          </Link>
        </div>
      </div>
    );
  }

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
          <h1 className="text-[40px] leading-[48px] font-bold text-[#111827] tracking-[-0.02em] mb-2">
            Reset your password
          </h1>
          <p className="text-[18px] leading-[28px] font-normal text-[#4B5563]">
            Enter your new password below.
          </p>
        </div>

        {/* Card with Brackets */}
        <div className="relative">
          {/* Brackets */}
          <div className="absolute -top-1 -left-1 w-6 h-6 border-t-[4px] border-l-[4px] border-[#6B7280]" />
          <div className="absolute -top-1 -right-1 w-6 h-6 border-t-[4px] border-r-[4px] border-[#6B7280]" />
          <div className="absolute -bottom-1 -left-1 w-6 h-6 border-b-[4px] border-l-[4px] border-[#6B7280]" />
          <div className="absolute -bottom-1 -right-1 w-6 h-6 border-b-[4px] border-r-[4px] border-[#6B7280]" />

          <div className="bg-[#F9FAFB] p-[32px] shadow-sm rounded-none border border-transparent relative z-10">
            {error && (
              <div className="p-3 bg-red-50 border border-red-200 text-red-700 text-sm rounded-[6px] mb-4">
                {error}
              </div>
            )}

            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="flex flex-col gap-1">
                <label className="text-[14px] font-medium text-[#4B5563]">New password</label>
                <input
                  type="password"
                  required
                  placeholder="••••••••••••"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="h-[44px] px-3 bg-[#E5E5E5] border border-[#9CA3AF] rounded-[6px] text-[#111827] focus:outline-none focus:ring-2 focus:ring-[#1E3A5F] focus:border-transparent placeholder-[#4B5563] transition-all"
                />
              </div>

              <div className="flex flex-col gap-1">
                <label className="text-[14px] font-medium text-[#4B5563]">Confirm password</label>
                <input
                  type="password"
                  required
                  placeholder="••••••••••••"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  className="h-[44px] px-3 bg-[#E5E5E5] border border-[#9CA3AF] rounded-[6px] text-[#111827] focus:outline-none focus:ring-2 focus:ring-[#1E3A5F] focus:border-transparent placeholder-[#4B5563] transition-all"
                />
              </div>

              <button
                type="submit"
                disabled={isLoading}
                className="relative w-full h-[48px] mt-2 bg-[#1E3A5F] hover:bg-[#152A45] text-white font-bold text-[16px] rounded-[6px] transition-all hover:scale-[1.01] flex items-center justify-center gap-2 disabled:opacity-70 disabled:hover:scale-100"
              >
                {isLoading ? 'Resetting...' : 'Reset password'}
              </button>
            </form>
          </div>
        </div>

        {/* Footer */}
        <div className="mt-8 text-center">
          <Link to="/login" className="text-[14px] text-[#1E3A5F] hover:underline">
            Back to login
          </Link>
        </div>
      </div>
    </div>
  );
}
