import { Link, useLocation } from 'react-router-dom';

export default function VerifyEmailLanding() {
  const location = useLocation();
  const message = location.state?.message || "Your email has been verified successfully.";

  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-[#F8F9FA] relative">
      <div 
        className="absolute inset-0 pointer-events-none"
        style={{
          backgroundSize: '24px 24px',
          backgroundImage: 'radial-gradient(circle, #E5E7EB 1px, transparent 1px)'
        }}
      />
      
      <div className="relative z-10 w-full max-w-[480px] px-6 text-center">
        <div className="bg-white/80 backdrop-blur-sm p-8 shadow-sm border border-[#E5E2DA] rounded-lg">
          <div className="w-16 h-16 bg-green-100 rounded-full flex items-center justify-center mx-auto mb-6">
            <svg className="w-8 h-8 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
          </div>
          
          <h2 className="text-2xl font-bold text-gray-900 mb-4">Account Verified</h2>
          <p className="text-gray-600 mb-8">{message}</p>
          
          <Link 
            to="/login"
            className="inline-flex items-center justify-center h-[48px] px-8 bg-[#1E3A5F] hover:bg-[#152A45] text-white font-semibold rounded-[6px] transition-all"
          >
            Continue to Log In
          </Link>
        </div>
      </div>
    </div>
  );
}
