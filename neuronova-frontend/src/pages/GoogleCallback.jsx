import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { authApi } from '../api/auth';

export default function GoogleCallback() {
  const navigate = useNavigate();
  const { setSession } = useAuth();
  const [error, setError] = useState('');
  const handledCallback = useRef(false);

  useEffect(() => {
    if (handledCallback.current) return;
    handledCallback.current = true;

    const queryParams = new URLSearchParams(window.location.search);
    const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ''));

    console.info('Google OAuth callback received', {
      pathname: window.location.pathname,
      hasQuery: Boolean(window.location.search),
      hasHash: Boolean(window.location.hash),
      queryParameterNames: [...queryParams.keys()],
      hashParameterNames: [...hashParams.keys()],
    });

    // Strip callback data immediately so credentials never linger in the URL or history.
    window.history.replaceState(null, '', window.location.pathname);

    const queryError = queryParams.get('error');
    if (queryError) {
      setError(queryError);
      return;
    }

    const accessToken = hashParams.get('access_token') ?? queryParams.get('access_token');
    const refreshToken = hashParams.get('refresh_token') ?? queryParams.get('refresh_token');

    if (!accessToken || !refreshToken) {
      setError('Missing tokens in OAuth callback.');
      return;
    }

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
  }, [navigate, setSession]);

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
