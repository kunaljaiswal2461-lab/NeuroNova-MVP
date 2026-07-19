import { api } from './client';

const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1';

export const authApi = {
  login: async (email, password) => {
    const res = await fetch(`${BASE_URL}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    
    if (!res.ok) {
      const isJson = res.headers.get('content-type')?.includes('application/json');
      if (isJson) {
        const errData = await res.json();
        throw Object.assign(new Error(errData.error?.message || errData.detail || 'Login failed'), { response: errData });
      }
      throw new Error(`Login failed with status ${res.status}`);
    }
    return res.json();
  },

  register: async (email, name, password) => {
    const res = await fetch(`${BASE_URL}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, name, password })
    });
    
    if (!res.ok) {
      const isJson = res.headers.get('content-type')?.includes('application/json');
      if (isJson) {
        const errData = await res.json();
        throw Object.assign(new Error(errData.error?.message || errData.detail || 'Registration failed'), { response: errData });
      }
      throw new Error(`Registration failed with status ${res.status}`);
    }
    return res.json();
  },

  logout: async (refreshToken) => {
    // Attempt to notify backend, don't fail if it doesn't work
    try {
      await fetch(`${BASE_URL}/auth/logout`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken })
      });
    } catch (e) {
      console.warn("Backend logout failed", e);
    }
  },

  getMe: async () => {
    return await api.get('/auth/me');
  },

  updateProfile: async (patch) => {
    const res = await fetch(`${BASE_URL}/auth/me`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('access_token')}`
      },
      body: JSON.stringify(patch)
    });
    if (!res.ok) {
      throw new Error('Failed to update profile');
    }
    return res.json();
  },

  forgotPassword: async (email) => {
    const res = await fetch(`${BASE_URL}/auth/forgot-password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email })
    });
    if (!res.ok) throw new Error('Failed to request password reset');
    return res.json();
  },

  googleStart: async () => {
    const res = await fetch(`${BASE_URL}/auth/google`);
    if (!res.ok) throw new Error('Failed to start Google sign-in');
    return res.json(); // { url } or { error }
  },

  resetPassword: async (token, new_password) => {
    const res = await fetch(`${BASE_URL}/auth/reset-password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, new_password })
    });
    if (!res.ok) throw new Error('Failed to reset password');
    return res.json();
  }
};
