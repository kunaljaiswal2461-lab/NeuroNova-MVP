import { lazy, Suspense, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import './index.css'

import { AuthProvider } from './context/AuthContext'
import { DatasetProvider } from './context/DatasetContext'
import DatasetRoute from './components/DatasetRoute'
import { ProtectedRoute } from './components/ProtectedRoute'
import { warmBackend } from './api/warmup'

// Route-level chunks keep heavyweight charts and database tooling out of the
// first page load. A route module is fetched only when its URL is visited.
const Landing = lazy(() => import('./pages/Landing'))
const Upload = lazy(() => import('./pages/Upload'))
const Explorer = lazy(() => import('./pages/Explorer'))
const Visualization = lazy(() => import('./pages/Visualization'))
const Insights = lazy(() => import('./pages/Insights'))
const Chat = lazy(() => import('./pages/Chat'))
const DatabaseChat = lazy(() => import('./pages/DatabaseChat'))
const Settings = lazy(() => import('./pages/Settings'))
const Login = lazy(() => import('./pages/Login'))
const Register = lazy(() => import('./pages/Register'))
const VerifyEmailLanding = lazy(() => import('./pages/VerifyEmailLanding'))
const GoogleCallback = lazy(() => import('./pages/GoogleCallback'))
const ForgotPassword = lazy(() => import('./pages/ForgotPassword'))
const ResetPassword = lazy(() => import('./pages/ResetPassword'))

function RouteLoading() {
  return <div role="status" aria-live="polite" className="app-route-loading">Loading…</div>
}

export default function App() {
  useEffect(() => {
    warmBackend()
  }, [])

  return (
    <BrowserRouter>
      <AuthProvider>
        <DatasetProvider>
          <Suspense fallback={<RouteLoading />}>
            <Routes>
              {/* Public Routes */}
              <Route path="/" element={<Landing />} />
              <Route path="/login" element={<Login />} />
              <Route path="/register" element={<Register />} />
              <Route path="/auth/verified" element={<VerifyEmailLanding />} />
              <Route path="/auth/google-callback" element={<GoogleCallback />} />
              <Route path="/auth/forgot" element={<ForgotPassword />} />
              <Route path="/auth/reset-password" element={<ResetPassword />} />

              <Route path="/sources/:sourceId/chat" element={<ProtectedRoute><DatabaseChat /></ProtectedRoute>} />

              <Route path="/upload" element={<ProtectedRoute><Upload /></ProtectedRoute>} />
              <Route path="/explorer" element={<ProtectedRoute><DatasetRoute><Explorer /></DatasetRoute></ProtectedRoute>} />
              <Route path="/visualization" element={<ProtectedRoute><DatasetRoute><Visualization /></DatasetRoute></ProtectedRoute>} />
              <Route path="/insights" element={<ProtectedRoute><DatasetRoute><Insights /></DatasetRoute></ProtectedRoute>} />
              <Route path="/chat" element={<ProtectedRoute><DatasetRoute><Chat /></DatasetRoute></ProtectedRoute>} />
              <Route path="/settings" element={<ProtectedRoute><Settings /></ProtectedRoute>} />

              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </DatasetProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
