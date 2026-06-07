import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import './index.css'

import { DatasetProvider } from './context/DatasetContext'
import Landing from './pages/Landing'
import Upload from './pages/Upload'
import Explorer from './pages/Explorer'
import Visualization from './pages/Visualization'
import Insights from './pages/Insights'
import Chat from './pages/Chat'

export default function App() {
  return (
    <BrowserRouter>
      <DatasetProvider>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/upload" element={<Upload />} />
          <Route path="/explorer" element={<Explorer />} />
          <Route path="/visualization" element={<Visualization />} />
          <Route path="/insights" element={<Insights />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </DatasetProvider>
    </BrowserRouter>
  )
}
