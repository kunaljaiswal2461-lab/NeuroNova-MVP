import { createContext, useContext, useState } from 'react'

const DatasetContext = createContext(null)

export function DatasetProvider({ children }) {
  const [activeDatasetId, setActiveDatasetId] = useState(null)
  const [activeDataset, setActiveDataset] = useState(null)

  function selectDataset(dataset) {
    setActiveDatasetId(dataset.dataset_id)
    setActiveDataset(dataset)
  }

  function clearDataset() {
    setActiveDatasetId(null)
    setActiveDataset(null)
  }

  return (
    <DatasetContext.Provider value={{ activeDatasetId, activeDataset, selectDataset, clearDataset }}>
      {children}
    </DatasetContext.Provider>
  )
}

export function useDataset() {
  const ctx = useContext(DatasetContext)
  if (!ctx) throw new Error('useDataset must be used within DatasetProvider')
  return ctx
}
