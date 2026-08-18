// The main thread's half of picking a folder.
//
// Split from `project.ts` so the worker can import the filtering without importing the code that
// starts a worker. What stays here is exactly what cannot leave: reading `webkitRelativePath` off each
// File, which is a DOM getter and only meaningful on the thread the picker handed the list to.
import { relPath, rootName, type Picked } from './project'
import type { PickRequest } from './project.worker'
import { once } from './worker'

/** Filter a picked folder, off the main thread. See `project.pick` for what "filter" means. */
export function pickOffThread(list: ArrayLike<File>, honorGitignore: boolean): Promise<Picked> {
  const files = Array.from(list)
  const req: PickRequest = { root: rootName(files), paths: files.map(relPath), files, honorGitignore }
  return once<Picked>(
    new Worker(new URL('./project.worker.ts', import.meta.url), { type: 'module' }),
    req,
  )
}
