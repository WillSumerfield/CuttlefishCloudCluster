// One job, one worker, then gone.
//
// Both of the upload path's expensive stages run off the main thread now — filtering a picked folder
// (project.worker) and building the gzip-tar body (tar.worker) — and they want the same plumbing:
// post one message, take one answer, terminate. Workers are built per job rather than kept resident,
// because both jobs are rare, discrete acts, and a resident worker holding the last project's Files
// alive costs more than starting one costs.
//
// The worker answers `{ value }` or `{ error }` rather than throwing: an exception inside a worker
// reaches the other side as a bare `error` event with no reason attached, and the reason is the whole
// point of the message (a path too long for ustar names the file it choked on).
export function once<T>(worker: Worker, message: unknown): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    worker.onmessage = (e: MessageEvent<{ value?: T; error?: string }>) => {
      worker.terminate()
      if (e.data.error) reject(new Error(e.data.error))
      else resolve(e.data.value as T)
    }
    // A worker that fails to LOAD (a stale cached bundle, a blocked module) never runs `onmessage`,
    // and without this the caller waits forever with its spinner up.
    worker.onerror = (e) => {
      worker.terminate()
      reject(new Error(e.message || 'the browser could not start a background task'))
    }
    worker.postMessage(message)
  })
}
