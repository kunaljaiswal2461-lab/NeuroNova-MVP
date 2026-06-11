import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import './index.css'

import { AuthProvider } from './context/AuthContext'
import { DatasetProvider } from './context/DatasetContext'
import { ProtectedRoute } from './components/ProtectedRoute'

import Landing from './pages/Landing'
import Upload from './pages/Upload'
import Explorer from './pages/Explorer'
import Visualization from './pages/Visualization'
import Insights from './pages/Insights'
import Chat from './pages/Chat'
import Login from './pages/Login'
import Register from './pages/Register'
import VerifyEmailLanding from './pages/VerifyEmailLanding'
import GoogleCallback from './pages/GoogleCallback'
import ForgotPassword from './pages/ForgotPassword'
import ResetPassword from './pages/ResetPassword'

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <DatasetProvider>
          <Routes>
            {/* Public Routes */}
            <Route path="/" element={<Landing />} />
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/auth/verified" element={<VerifyEmailLanding />} />
            <Route path="/auth/google-callback" element={<GoogleCallback />} />
            <Route path="/auth/forgot" element={<ForgotPassword />} />
            <Route path="/auth/reset-password" element={<ResetPassword />} />

            {/* Protected Routes */}
            <Route path="/upload" element={<ProtectedRoute><Upload /></ProtectedRoute>} />
            <Route path="/explorer" element={<ProtectedRoute><Explorer /></ProtectedRoute>} />
            <Route path="/visualization" element={<ProtectedRoute><Visualization /></ProtectedRoute>} />
            <Route path="/insights" element={<ProtectedRoute><Insights /></ProtectedRoute>} />
            <Route path="/chat" element={<ProtectedRoute><Chat /></ProtectedRoute>} />

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </DatasetProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
