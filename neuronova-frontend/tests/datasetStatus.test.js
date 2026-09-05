import { afterEach, describe, expect, it, vi } from 'vitest'
import { MAX_UPLOAD_BYTES, UPLOAD_LIMIT_MESSAGE, isDatasetComplete, pollDatasetStatus, validateUpload } from '../src/context/datasetStatus'

const flush = async () => { await Promise.resolve(); await Promise.resolve() }

afterEach(() => vi.useRealTimers())

describe('pilot dataset status', () => {
  it('accepts exactly 20 MiB and rejects one byte over', () => {
    expect(validateUpload({ size: MAX_UPLOAD_BYTES })).toBeNull()
    expect(validateUpload({ size: MAX_UPLOAD_BYTES + 1 })).toBe(UPLOAD_LIMIT_MESSAGE)
  })

  it('only unlocks COMPLETE with a dataset ID', () => {
    for (const status of ['UPLOADING', 'UPLOADED', 'QUEUED', 'PROFILING', 'FINDINGS', 'VIZ', 'INSIGHTS', 'INDEXING', 'FAILED', undefined]) {
      expect(isDatasetComplete({ dataset_id: 'one', status })).toBe(false)
    }
    expect(isDatasetComplete({ dataset_id: 'one', status: 'COMPLETE' })).toBe(true)
    expect(isDatasetComplete({ status: 'COMPLETE' })).toBe(false)
  })

  it('checks immediately, waits five seconds after settlement, never overlaps, and stops COMPLETE', async () => {
    vi.useFakeTimers()
    let resolveFirst
    const get = vi.fn().mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve }))
      .mockResolvedValue({ status: 'COMPLETE' })
    const onComplete = vi.fn()
    const stop = pollDatasetStatus({ id: 'one', get, onStatus: vi.fn(), onMessage: vi.fn(), onComplete })
    expect(get).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(20000)
    expect(get).toHaveBeenCalledTimes(1)
    resolveFirst({ status: 'QUEUED' })
    await flush()
    await vi.advanceTimersByTimeAsync(4999)
    expect(get).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(get).toHaveBeenCalledTimes(2)
    expect(onComplete).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(20000)
    expect(get).toHaveBeenCalledTimes(2)
    stop()
  })

  it('stops FAILED without loading artifacts or metadata', async () => {
    vi.useFakeTimers()
    const get = vi.fn().mockResolvedValue({ status: 'FAILED', error_message: 'Interrupted' })
    const onComplete = vi.fn()
    pollDatasetStatus({ id: 'one', get, onStatus: vi.fn(), onMessage: vi.fn(), onComplete })
    await vi.advanceTimersByTimeAsync(20000)
    expect(get).toHaveBeenCalledTimes(1)
    expect(onComplete).not.toHaveBeenCalled()
  })

  it('aborts in-flight work and ignores a late result', async () => {
    vi.useFakeTimers()
    let resolve
    const get = vi.fn(() => new Promise(done => { resolve = done }))
    const onStatus = vi.fn()
    const stop = pollDatasetStatus({ id: 'one', get, onStatus, onMessage: vi.fn(), onComplete: vi.fn() })
    const signal = get.mock.calls[0][1].signal
    stop()
    expect(signal.aborted).toBe(true)
    resolve({ status: 'COMPLETE' })
    await vi.advanceTimersByTimeAsync(20000)
    expect(onStatus).not.toHaveBeenCalled()
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('reports unavailability without marking the pipeline FAILED, then recovers', async () => {
    vi.useFakeTimers()
    const get = vi.fn().mockRejectedValueOnce(new Error('Processing may still be running. Please try again shortly.'))
      .mockResolvedValue({ status: 'PROFILING', progress_pct: 30 })
    const onStatus = vi.fn()
    const onMessage = vi.fn()
    const stop = pollDatasetStatus({ id: 'one', get, onStatus, onMessage, onComplete: vi.fn() })
    await flush()
    expect(onStatus).not.toHaveBeenCalled()
    expect(onMessage).toHaveBeenCalledWith('Processing may still be running. Please try again shortly.')
    await vi.advanceTimersByTimeAsync(5000)
    expect(onStatus).toHaveBeenCalledWith({ status: 'PROFILING', progress_pct: 30 })
    expect(onMessage).toHaveBeenLastCalledWith(null)
    stop()
  })
})
