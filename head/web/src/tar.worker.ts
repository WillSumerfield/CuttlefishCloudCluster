/// <reference lib="webworker" />
//
// The upload body, built off the main thread.
//
// `makeTar` reads nothing and copies nothing, so for a while it looked as though the tar was free and
// the tab's freeze had been fixed by making it so. It was not: `CompressionStream` runs its deflate on
// **the thread that created it**, and `gzip()` pulls the entire project through it before `fetch` is
// ever called. On a project holding a few hundred MB — a checkpoint, a dataset, the 300MB blob a test
// writes — that is tens of seconds of solid main-thread CPU, and the worst case is incompressible
// bytes, which cost full deflate and produce nothing. Every animated surface in the app is a
// `requestAnimationFrame` on that same thread (app/AbyssBackground, tide/TideHorizon, queue/IridBloom),
// so the page does not merely stutter — it stops.
//
// Here instead, where the only cost to the tab is the postMessage at each end. `File` is structured-
// cloneable and its contents stay on disk, so handing the list over copies references, not bytes.
import { gzip, makeTar } from './tar'

export type TarRequest = { paths: string[]; files: File[] }

self.onmessage = async (e: MessageEvent<TarRequest>) => {
  const { paths, files } = e.data
  try {
    const body = await gzip(makeTar(files.map((f, i) => ({ path: paths[i], data: f }))))
    // The Blob goes back by reference too — the browser keeps it in blob storage, and a large one is
    // backed by disk rather than by either thread's heap.
    self.postMessage({ body })
  } catch (err) {
    // A path too long for ustar is the one error `makeTar` raises, and it names the file. It has to
    // cross the thread boundary as a message: an exception in here would otherwise surface on the main
    // thread as a bare "error" event with no reason attached.
    self.postMessage({ error: err instanceof Error ? err.message : String(err) })
  }
}
