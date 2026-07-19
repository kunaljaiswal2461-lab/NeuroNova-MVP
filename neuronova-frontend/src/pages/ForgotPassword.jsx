import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { authApi } from '../api/auth';

export default function ForgotPassword() {
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setIsLoading(true);
    try {
      await authApi.forgotPassword(email);
      setSubmitted(true);
    } catch (err) {
      console.error('Forgot password error:', err);
    } finally {
      setIsLoading(false);
    }
  };

  if (submitted) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-[#F8F9FA] px-6">
        <div className="max-w-md text-center">
          <div className="mb-6 flex justify-center">
            <svg className="w-16 h-16 text-[#34A853]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
          </div>
          <h1 className="text-2xl font-bold text-[#111827] mb-2">Check your inbox</h1>
          <p className="text-[#4B5563] mb-8">
            If that email is registered, we've sent a password reset link.
          </p>
          <Link
            to="/login"
            className="inline-block px-5 h-11 bg-[#1E3A5F] hover:bg-[#152A45] text-white font-semibold rounded-[6px]"
          >
            Back to login
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
            Forgot your password?
          </h1>
          <p className="text-[18px] leading-[28px] font-normal text-[#4B5563]">
            Enter your email to receive a password reset link.
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
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="flex flex-col gap-1">
                <label className="text-[14px] font-medium text-[#4B5563]">Work email</label>
                <input
                  type="email"
                  required
                  placeholder="you@work-email.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="h-[44px] px-3 bg-[#E5E5E5] border border-[#9CA3AF] rounded-[6px] text-[#111827] focus:outline-none focus:ring-2 focus:ring-[#1E3A5F] focus:border-transparent placeholder-[#4B5563] transition-all"
                />
              </div>

              <button
                type="submit"
                disabled={isLoading}
                className="relative w-full h-[48px] mt-2 bg-[#1E3A5F] hover:bg-[#152A45] text-white font-bold text-[16px] rounded-[6px] transition-all hover:scale-[1.01] flex items-center justify-center gap-2 disabled:opacity-70 disabled:hover:scale-100"
              >
                {isLoading ? 'Sending...' : 'Send reset link'}
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
