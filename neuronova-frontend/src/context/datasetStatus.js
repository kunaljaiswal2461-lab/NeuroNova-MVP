export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024
export const UPLOAD_LIMIT_MESSAGE = 'Pilot version currently supports datasets up to 20 MB.'
export const STATUS_UNAVAILABLE_MESSAGE = 'Processing may still be running. Please try again shortly.'
export const POLL_INTERVAL_MS = 5000

export function validateUpload(file) {
  return file.size > MAX_UPLOAD_BYTES ? UPLOAD_LIMIT_MESSAGE : null
}

export function isDatasetComplete(dataset) {
  return Boolean(dataset?.dataset_id && dataset.status === 'COMPLETE')
}

export function isTerminalStatus(status) {
  return status === 'COMPLETE' || status === 'FAILED'
}

export function stageLabel(status) {
  return {
    UPLOADING: 'Uploading', UPLOADED: 'Uploaded', QUEUED: 'Queued',
    PROFILING: 'Profiling', FINDINGS: 'Extracting findings', VIZ: 'Building visualizations',
    INSIGHTS: 'Generating insights', INDEXING: 'Indexing', COMPLETE: 'Complete', FAILED: 'Failed',
  }[status] || 'Waiting for status'
}

// One request at a time. Schedule from settlement, not from request start, so a
// slow backend (including API retries) never accumulates overlapping polls.
export function pollDatasetStatus({ id, get, onStatus, onMessage, onComplete,
  schedule = setTimeout, unschedule = clearTimeout }) {
  const controller = new AbortController()
  let timer
  const { signal } = controller
  async function check() {
    let terminal = false
    try {
      const status = await get(`/datasets/${id}/status`, {
        signal,
        onRetry: message => { if (!signal.aborted) onMessage(message) },
      })
      if (signal.aborted) return
      terminal = isTerminalStatus(status.status)
      onMessage(null)
      onStatus(status)
      if (status.status === 'COMPLETE') await onComplete(signal)
    } catch (error) {
      if (!signal.aborted) onMessage(error.message || STATUS_UNAVAILABLE_MESSAGE)
    } finally {
      if (!signal.aborted && !terminal) timer = schedule(check, POLL_INTERVAL_MS)
    }
  }
  check()
  return () => {
    controller.abort()
    unschedule(timer)
  }
}
