const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1'
const FALLBACK_API_KEY = import.meta.env.VITE_API_KEY || 'dev-local-key-change-me'

// Local queue for concurrent 401s
let isRefreshing = false;
let failedQueue = [];

function processQueue(error, token = null) {
  failedQueue.forEach(prom => {
    if (error) {
      prom.reject(error);
    } else {
      prom.resolve(token);
    }
  });
  failedQueue = [];
}

function authHeaders() {
  const token = localStorage.getItem('access_token');
  if (token) {
    return { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };
  }
  // Fallback for non-authenticated scripts if needed
  return { 'X-API-Key': FALLBACK_API_KEY, 'Content-Type': 'application/json' };
}

async function handleRefresh() {
  const refreshToken = localStorage.getItem('refresh_token');
  if (!refreshToken) throw new Error("No refresh token available");

  const res = await fetch(`${BASE_URL}/auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken })
  });

  if (!res.ok) {
    throw new Error('Refresh failed');
  }
  const data = await res.json();
  localStorage.setItem('access_token', data.access_token);
  return data.access_token;
}

async function fetchWithIntercept(url, options) {
  let res = await fetch(url, options);

  if (res.status === 401) {
    const refreshToken = localStorage.getItem('refresh_token');
    if (!refreshToken) {
      const msg = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${msg}`);
    }

    if (isRefreshing) {
      return new Promise(function(resolve, reject) {
        failedQueue.push({ resolve, reject });
      }).then(token => {
        options.headers['Authorization'] = 'Bearer ' + token;
        return fetch(url, options);
      }).catch(err => {
        return Promise.reject(err);
      });
    }

    isRefreshing = true;

    try {
      const newToken = await handleRefresh();
      processQueue(null, newToken);
      options.headers['Authorization'] = 'Bearer ' + newToken;
      res = await fetch(url, options);
    } catch (err) {
      processQueue(err, null);
      localStorage.removeItem('access_token');
      localStorage.removeItem('refresh_token');
      window.location.href = '/login';
      return Promise.reject(err);
    } finally {
      isRefreshing = false;
    }
  }

  if (!res.ok) {
    const isJson = res.headers.get('content-type')?.includes('application/json');
    if (isJson) {
      const errData = await res.json();
      throw Object.assign(new Error(errData.error?.message || errData.detail || 'Request failed'), { response: errData });
    }
    const msg = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${msg}`);
  }

  return res;
}


async function get(path) {
  const res = await fetchWithIntercept(`${BASE_URL}${path}`, { headers: authHeaders() })
  return res.json()
}

async function getBlob(path) {
  const res = await fetchWithIntercept(`${BASE_URL}${path}`, { headers: authHeaders() })
  return res.blob()
}

async function post(path, body = {}) {
  const res = await fetchWithIntercept(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  })
  return res.json()
}

async function postForm(path, formData) {
  const headers = authHeaders();
  delete headers['Content-Type']; // Let browser set multipart/form-data boundary
  
  const res = await fetchWithIntercept(`${BASE_URL}${path}`, {
    method: 'POST',
    headers,
    body: formData,
  })
  return res.json()
}

async function* streamSSE(path, body = {}) {
  // Simple fetch without interceptor for SSE for simplicity, 
  // or wrap in interceptor if we want auto-refresh on chat streams.
  // Using fetchWithIntercept to handle 401 on initial connection:
  const res = await fetchWithIntercept(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  })

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? ''
    for (const part of parts) {
      if (!part.trim()) continue
      const eventMatch = part.match(/^event:\s*(.+)$/m)
      const dataMatch = part.match(/^data:\s*(.+)$/ms)
      if (eventMatch && dataMatch) {
        try {
          yield { event: eventMatch[1].trim(), data: JSON.parse(dataMatch[1]) }
        } catch {
          // skip malformed frame
        }
      }
    }
  }
}

export const api = { get, getBlob, post, postForm, streamSSE }
