import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { authApi } from '../api/auth';

export default function GoogleCallback() {
  const navigate = useNavigate();
  const { setSession } = useAuth();
  const [searchParams] = useSearchParams();
  const [error, setError] = useState('');

  useEffect(() => {
    const queryError = searchParams.get('error');
    if (queryError) {
      setError(queryError);
      return;
    }

    const hash = window.location.hash.startsWith('#')
      ? window.location.hash.slice(1)
      : window.location.hash;
    const params = new URLSearchParams(hash);
    const accessToken = params.get('access_token');
    const refreshToken = params.get('refresh_token');

    if (!accessToken || !refreshToken) {
      setError('Missing tokens in OAuth callback.');
      return;
    }

    // Strip the fragment so tokens don't linger in the URL bar / history.
    window.history.replaceState(null, '', window.location.pathname);

    (async () => {
      try {
        localStorage.setItem('access_token', accessToken);
        localStorage.setItem('refresh_token', refreshToken);
        const user = await authApi.getMe();
        setSession({ accessToken, user });
        navigate('/upload', { replace: true });
      } catch (err) {
        localStorage.removeItem('access_token');
        localStorage.removeItem('refresh_token');
        setError(err.message || 'Failed to complete Google sign-in.');
      }
    })();
  }, [navigate, searchParams, setSession]);

  if (error) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-[#F8F9FA] px-6">
        <div className="max-w-md text-center">
          <h1 className="text-2xl font-bold text-[#111827] mb-2">Sign-in failed</h1>
          <p className="text-[#4B5563] mb-6">{error}</p>
          <button
            onClick={() => navigate('/login', { replace: true })}
            className="px-5 h-11 bg-[#1E3A5F] hover:bg-[#152A45] text-white font-semibold rounded-[6px]"
          >
            Back to login
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#F8F9FA]">
      <p className="text-[#4B5563]">Completing sign-in…</p>
    </div>
  );
}
