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
  bytes: number   // total size of `files`
  scanned: number // how many were in the folder before filtering
}

/** The path of `f` relative to the picked folder root ("proj/src/a.py" -> "src/a.py"). */
export function relPath(f: File): string {
  const p = f.webkitRelativePath || f.name
  const i = p.indexOf('/')
  return i < 0 ? p : p.slice(i + 1)
}

function rootName(files: File[]): string {
  const p = files[0]?.webkitRelativePath ?? ''
  return p.split('/')[0] || 'project'
}

/**
 * Filter a picked folder. `honorGitignore=false` uploads everything except .git/ — which is never
 * uploaded under any setting: it is the repo's entire object store, it can dwarf the working tree, and
 * a Spawn has no use for it.
 */
export async function pick(fileList: FileList, honorGitignore: boolean): Promise<Picked> {
  const all = Array.from(fileList)
  const notGit = all.filter((f) => !relPath(f).startsWith('.git/'))

  if (!honorGitignore) {
    return { root: rootName(all), files: notGit, bytes: total(notGit), scanned: all.length }
  }

  // Collect every .gitignore in the tree. Patterns from a nested .gitignore are scoped to its own
  // directory, so prefix them with that directory to keep them from matching the whole project.
  const ig = ignore()
  for (const f of notGit.filter((f) => f.name === '.gitignore')) {
    const dir = relPath(f).slice(0, -'.gitignore'.length) // "" at the root, "sub/" when nested
    const patterns = (await f.text())
      .split('\n')
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith('#'))
      .map((l) => (dir ? scopePattern(l, dir) : l))
    ig.add(patterns)
  }

  const files = notGit.filter((f) => !ig.ignores(relPath(f)))
  return { root: rootName(all), files, bytes: total(files), scanned: all.length }
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
