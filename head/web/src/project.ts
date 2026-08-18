// Turning a picked folder into the set of files we'll upload.
//
// The CLI gets exact parity by shelling out to `git ls-files --cached --others
// --exclude-standard`. A browser can't: there's no git to run, and git's *global* ignore file
// (~/.config/git/ignore) lives outside the picked folder — unreachable from a web page by
// construction. So we approximate with the same .gitignore semantics (the `ignore` package), and
// make the result VISIBLE: the UI shows file count and total size and uploads nothing until
// confirmed. A wrong answer you can see before 4 GB moves is very different from one you can't.

import ignore from 'ignore'

export type Picked = {
  root: string
  files: File[]   // what we'd upload
  // Each file's path relative to the picked root, in step with `files`. Carried rather than re-derived
  // because `webkitRelativePath` DOES NOT SURVIVE structured clone: a File that has crossed to a worker
  // still knows its name and its bytes, and no longer knows where in the tree it came from. Every path
  // is therefore read once, on the thread that owns the picker, and travels alongside.
  paths: string[]
  bytes: number   // total size of `files`
  scanned: number // how many were in the folder before filtering
}

/** The path of `f` relative to the picked folder root ("proj/src/a.py" -> "src/a.py"). */
export function relPath(f: File): string {
  const p = f.webkitRelativePath || f.name
  const i = p.indexOf('/')
  return i < 0 ? p : p.slice(i + 1)
}

/** The folder's own name, off the first file's full path. Read on the picker's thread, like `relPath`
 *  and for the same reason — `webkitRelativePath` does not cross to a worker. */
export function rootName(files: ArrayLike<File>): string {
  const p = files[0]?.webkitRelativePath ?? ''
  return p.split('/')[0] || 'project'
}

/**
 * Filter a picked folder. `honorGitignore=false` uploads everything except .git/ — which is never
 * uploaded under any setting: it is the repo's entire object store, it can dwarf the working tree, and
 * a Spawn has no use for it.
 */
export async function pick(
  root: string,
  paths: string[],
  all: File[],
  honorGitignore: boolean,
): Promise<Picked> {
  const scanned = all.length
  const keep: number[] = []
  for (let i = 0; i < scanned; i++) if (!paths[i].startsWith('.git/')) keep.push(i)

  if (!honorGitignore) {
    const files = keep.map((i) => all[i])
    return { root, files, paths: keep.map((i) => paths[i]), bytes: total(files), scanned }
  }

  // Collect every .gitignore in the tree. Patterns from a nested .gitignore are scoped to its own
  // directory, so prefix them with that directory to keep them from matching the whole project.
  const ig = ignore()
  for (const i of keep) {
    if (all[i].name !== '.gitignore') continue
    const dir = paths[i].slice(0, -'.gitignore'.length) // "" at the root, "sub/" when nested
    const patterns = (await all[i].text())
      .split('\n')
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith('#'))
      .map((l) => (dir ? scopePattern(l, dir) : l))
    ig.add(patterns)
  }

  const kept = keep.filter((i) => !ig.ignores(paths[i]))
  const files = kept.map((i) => all[i])
  return { root, files, paths: kept.map((i) => paths[i]), bytes: total(files), scanned }
}

/** Re-anchor a nested .gitignore's pattern to its directory, preserving negation ('!'). */
function scopePattern(pattern: string, dir: string): string {
  if (pattern.startsWith('!')) return '!' + dir + pattern.slice(1).replace(/^\//, '')
  return dir + pattern.replace(/^\//, '')
}

function total(files: File[]): number {
  return files.reduce((n, f) => n + f.size, 0)
}

export function human(bytes: number): string {
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let n = bytes
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024
    i++
  }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${u[i]}`
}
