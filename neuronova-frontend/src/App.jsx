import { useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate, Outlet } from 'react-router-dom'
import './index.css'

import { AuthProvider } from './context/AuthContext'
import { DatasetProvider } from './context/DatasetContext'
import DatasetRoute from './components/DatasetRoute'
import { ProtectedRoute } from './components/ProtectedRoute'
import { warmBackend } from './api/warmup'

import Landing from './pages/Landing'
import Upload from './pages/Upload'
import Explorer from './pages/Explorer'
import Visualization from './pages/Visualization'
import Insights from './pages/Insights'
import Chat from './pages/Chat'
import DatabaseChat from './pages/DatabaseChat'
import Settings from './pages/Settings'
import Login from './pages/Login'
import Register from './pages/Register'
import VerifyEmailLanding from './pages/VerifyEmailLanding'
import GoogleCallback from './pages/GoogleCallback'
import ForgotPassword from './pages/ForgotPassword'
import ResetPassword from './pages/ResetPassword'

// Uploaded-file screens share the dataset context. Source-bound PostgreSQL
// chat deliberately does not: a selected source is neither an uploaded
// dataset nor an authority to reuse dataset state.
function DatasetWorkspace() {
  return <DatasetProvider><Outlet /></DatasetProvider>
}

export default function App() {
  useEffect(() => {
    warmBackend()
  }, [])

  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          {/* Public Routes */}
          <Route path="/" element={<Landing />} />
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route path="/auth/verified" element={<VerifyEmailLanding />} />
          <Route path="/auth/google-callback" element={<GoogleCallback />} />
          <Route path="/auth/forgot" element={<ForgotPassword />} />
          <Route path="/auth/reset-password" element={<ResetPassword />} />

          {/* This source-only route stays outside DatasetWorkspace. */}
          <Route path="/sources/:sourceId/chat" element={<ProtectedRoute><DatabaseChat /></ProtectedRoute>} />

          {/* Uploaded-file workspace routes retain one shared dataset context. */}
          <Route element={<DatasetWorkspace />}>
            <Route path="/upload" element={<ProtectedRoute><Upload /></ProtectedRoute>} />
            <Route path="/explorer" element={<ProtectedRoute><DatasetRoute><Explorer /></DatasetRoute></ProtectedRoute>} />
            <Route path="/visualization" element={<ProtectedRoute><DatasetRoute><Visualization /></DatasetRoute></ProtectedRoute>} />
            <Route path="/insights" element={<ProtectedRoute><DatasetRoute><Insights /></DatasetRoute></ProtectedRoute>} />
            <Route path="/chat" element={<ProtectedRoute><DatasetRoute><Chat /></DatasetRoute></ProtectedRoute>} />
            <Route path="/settings" element={<ProtectedRoute><Settings /></ProtectedRoute>} />
          </Route>

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
