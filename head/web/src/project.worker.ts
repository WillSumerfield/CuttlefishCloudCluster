/// <reference lib="webworker" />
//
// Filtering a picked folder, off the main thread.
//
// Everything `pick` does is per-file and synchronous: a `.git/` test, a `.gitignore` match through the
// `ignore` package, a size sum. None of it is slow for a folder of ten files and all of it is for a
// real project — 50k paths against a modest .gitignore measures in the hundreds of milliseconds, and
// that is before Firefox's own overhead. Picking a folder is the first thing a user does here, so the
// page froze on the way IN, long before anything was uploaded.
//
// Paths arrive already resolved. `webkitRelativePath` is not part of a File's structured clone, so a
// File that has crossed to this thread no longer knows where in the tree it lived — see `Picked.paths`.
import { pick, type Picked } from './project'

export type PickRequest = { root: string; paths: string[]; files: File[]; honorGitignore: boolean }

self.onmessage = async (e: MessageEvent<PickRequest>) => {
  const { root, paths, files, honorGitignore } = e.data
  try {
    self.postMessage({ value: await pick(root, paths, files, honorGitignore) satisfies Picked })
  } catch (err) {
    self.postMessage({ error: err instanceof Error ? err.message : String(err) })
  }
}
