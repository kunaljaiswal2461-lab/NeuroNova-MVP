import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { matchPath, useLocation } from 'react-router-dom'
import { api } from '../api/client'
import { isDatasetComplete, isTerminalStatus, pollDatasetStatus } from './datasetStatus'

const DatasetContext = createContext(null)
const WORKSPACE_PATHS = ['/upload', '/explorer', '/insights', '/visualization', '/chat']

export function DatasetProvider({ children }) {
  const [activeDataset, setActiveDataset] = useState(null)
  const [selectionVersion, setSelectionVersion] = useState(0)
  const [statusMessage, setStatusMessage] = useState(null)
  const datasetRef = useRef(null)
  const cancelPollRef = useRef(null)
  const { pathname } = useLocation()
  const activeDatasetId = activeDataset?.dataset_id ?? null

  const selectDataset = useCallback(dataset => {
    cancelPollRef.current?.()
    datasetRef.current = dataset
    setActiveDataset(dataset)
    setStatusMessage(null)
    setSelectionVersion(version => version + 1)
  }, [])

  const clearDataset = useCallback(() => selectDataset(null), [selectDataset])

  // Metadata/health updates must never reselect an old dataset or restart polling.
  const updateDataset = useCallback((id, patch) => {
    if (datasetRef.current?.dataset_id !== id) return
    const updated = { ...datasetRef.current, ...patch }
    datasetRef.current = updated
    setActiveDataset(updated)
  }, [])

  useEffect(() => {
    const dataset = datasetRef.current
    // Match the router's case-insensitive, trailing-slash-tolerant routes.
    if (!activeDatasetId || !WORKSPACE_PATHS.some(path => matchPath(path, pathname))) return
    if (isTerminalStatus(dataset?.status)) return
    const cancel = pollDatasetStatus({
      id: activeDatasetId,
      get: (path, options) => api.get(path, options),
      onStatus: status => updateDataset(activeDatasetId, status),
      onMessage: setStatusMessage,
      onComplete: async signal => {
        try {
          const metadata = await api.get(`/datasets/${activeDatasetId}`, { signal })
          if (!signal.aborted) updateDataset(activeDatasetId, metadata)
        } catch {
          if (!signal.aborted) setStatusMessage('Processing is complete, but dataset details are temporarily unavailable. Please refresh the dataset list.')
        }
      },
    })
    cancelPollRef.current = cancel
    return cancel
  }, [activeDatasetId, selectionVersion, pathname, updateDataset])

  return (
    <DatasetContext.Provider value={{ activeDatasetId, activeDataset, selectDataset, clearDataset,
      updateDataset, statusMessage, datasetReady: isDatasetComplete(activeDataset) }}>
      {children}
    </DatasetContext.Provider>
  )
}

export function useDataset({ optional = false } = {}) {
  const ctx = useContext(DatasetContext)
  if (!ctx && !optional) throw new Error('useDataset must be used within DatasetProvider')
  return ctx
}
