import { createContext, useContext, useState, useEffect } from 'react';
import { authApi } from '../api/auth';

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [accessToken, setAccessToken] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const initAuth = async () => {
      const token = localStorage.getItem('access_token');
      if (token) {
        setAccessToken(token);
        try {
          // Verify token by fetching user profile
          const userData = await authApi.getMe();
          setUser(userData);
        } catch (error) {
          console.error("Failed to restore session", error);
          // If refresh logic in client.js fails, it will redirect to /login and clear tokens.
          // But here, we can just clear state if it's completely unrecoverable.
          setUser(null);
          setAccessToken(null);
        }
      }
      setLoading(false);
    };

    initAuth();
  }, []);

  const login = async (email, password) => {
    const data = await authApi.login(email, password);
    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    setAccessToken(data.access_token);
    setUser(data.user);
  };

  const register = async (email, name, password) => {
    await authApi.register(email, name, password);
  };

  const logout = async () => {
    const refresh = localStorage.getItem('refresh_token');
    if (refresh) {
      await authApi.logout(refresh);
    }
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    setAccessToken(null);
    setUser(null);
    window.location.href = '/login';
  };

  // Used by OAuth callback pages that already have tokens + user in hand.
  const setSession = ({ accessToken: token, user: u }) => {
    setAccessToken(token);
    setUser(u);
  };

  const updateProfile = async (patch) => {
    const updatedUser = await authApi.updateProfile(patch);
    setUser(updatedUser);
    return updatedUser;
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        accessToken,
        loading,
        login,
        register,
        logout,
        setSession,
        updateProfile,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
