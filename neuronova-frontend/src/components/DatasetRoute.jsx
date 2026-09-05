import { Fragment } from 'react'
import { Link } from 'react-router-dom'
import Layout from './Layout'
import { useDataset } from '../context/DatasetContext'
import { stageLabel } from '../context/datasetStatus'

// Do not mount analytics children before COMPLETE: hiding their UI alone would
// still run effects that fetch artifacts (or create chat sessions).
export default function DatasetRoute({ children }) {
  const { activeDataset, datasetReady, statusMessage } = useDataset()
  if (datasetReady) return <Fragment key={activeDataset.dataset_id}>{children}</Fragment>

  const failed = activeDataset?.status === 'FAILED'
  return (
    <Layout className="dataset-workflow" title="Dataset processing" subtitle={activeDataset?.original_name}>
      <div className="card card-pad" style={{ maxWidth: 820 }}>
        <h2>{activeDataset ? stageLabel(activeDataset.status) : 'No Dataset Selected'}</h2>
        <p role="status" style={{ margin: '12px 0' }}>
          {failed
            ? (activeDataset.error_message || 'Processing failed. Please upload the dataset again.')
            : activeDataset
              ? `Analysis will unlock automatically when processing is complete. Progress: ${activeDataset.progress_pct ?? 0}%.`
              : 'Upload or select a completed dataset to begin analysis.'}
        </p>
        {statusMessage && <p role="status" style={{ marginBottom: 12 }}>{statusMessage}</p>}
        <Link to="/upload" className="btn btn-primary">
          {failed ? 'Upload dataset again' : 'Go to Upload Center'}
        </Link>
      </div>
    </Layout>
  )
}
