// A minimal tar (ustar) writer, so the browser can POST exactly what the CLI POSTs.
//
// The Head API's /submit takes one body format: a gzip-tar of the project. Rather than teach the
// server a second, browser-only format (multipart), we build the same bytes here. Tar is why that's
// cheap: it's just a 512-byte header per file, the file content padded up to a 512-byte boundary, and
// two zero blocks to finish. Gzip then comes free from the platform (CompressionStream).
//
// Deliberately not general: no symlinks, no long-name (PAX) records, no mode bits worth speaking of.
// A browser File carries no permissions anyway, so every entry is written 0644 — the one real
// divergence from the CLI's tarball, and harmless because a Spawn runs `python <script>`, never
// `./<script>`.

const BLOCK = 512

/** One file in the archive. `data` is a Blob — normally the picked `File` itself, unread. */
export type TarEntry = { path: string; data: Blob }

// One shared block of zeros to pad and terminate with. Blob copies what it is given at construction,
// so handing the same buffer out repeatedly is safe and costs one 512-byte allocation for a whole tar.
const ZEROS = new Uint8Array(BLOCK)

function octal(n: number, len: number): string {
  // tar stores numbers as zero-padded octal, NUL-terminated: len-1 digits + '\0'.
  return n.toString(8).padStart(len - 1, '0') + '\0'
}

function writeStr(buf: Uint8Array, offset: number, s: string, max: number) {
  const bytes = new TextEncoder().encode(s)
  if (bytes.length > max) throw new Error(`tar: field too long (${s})`)
  buf.set(bytes, offset)
}

function header(path: string, size: number): Uint8Array {
  const h = new Uint8Array(BLOCK)
  // ustar splits long paths across name[100] + prefix[155]. Split on a '/' so both halves are valid.
  let name = path
  let prefix = ''
  if (name.length > 100) {
    const cut = path.lastIndexOf('/', path.length - 101 + 100)
    if (cut < 0) throw new Error(`tar: path too long (${path})`)
    prefix = path.slice(0, cut)
    name = path.slice(cut + 1)
    if (name.length > 100 || prefix.length > 155) throw new Error(`tar: path too long (${path})`)
  }
  writeStr(h, 0, name, 100)           // name
  writeStr(h, 100, octal(0o644, 8), 8)   // mode  (browsers give us no mode bits)
  writeStr(h, 108, octal(0, 8), 8)       // uid   (the Head unpacks as the Mantle regardless)
  writeStr(h, 116, octal(0, 8), 8)       // gid
  writeStr(h, 124, octal(size, 12), 12)  // size
  writeStr(h, 136, octal(0, 12), 12)     // mtime (0: the Head only reads content)
  h[156] = 0x30                          // typeflag '0' = regular file
  writeStr(h, 257, 'ustar\0', 6)         // magic
  writeStr(h, 263, '00', 2)              // version
  writeStr(h, 345, prefix, 155)          // prefix

  // Checksum is computed with the checksum field itself treated as 8 spaces, then written back in.
  h.fill(0x20, 148, 156)
  let sum = 0
  for (const b of h) sum += b
  writeStr(h, 148, octal(sum, 7), 7)
  h[155] = 0x20
  return h
}

/**
 * Build an uncompressed tar of `entries` as a Blob. Paths must be relative (no leading '/').
 *
 * **Nothing is read and nothing is copied**, and that is the whole point. This used to allocate one
 * `Uint8Array` the size of the entire project and memcpy every file into it, after the caller had
 * already pulled each file into memory with `await file.arrayBuffer()`. Peak was ~2x the project, and
 * because both loops are synchronous the tab painted nothing and answered no clicks for the duration —
 * picking a folder of any size froze the page.
 *
 * A Blob is a *reference* to bytes, not the bytes: the browser keeps a File's contents on disk and
 * reads them only when something drains the Blob's stream. A tar header needs just the size, and
 * `File.size` is already known, so the whole archive can be described without touching a byte of it.
 * The read then happens lazily, off the main thread, as gzip pulls.
 */
export function makeTar(entries: TarEntry[]): Blob {
  const pad = (n: number) => (BLOCK - (n % BLOCK)) % BLOCK
  const parts: BlobPart[] = []
  for (const e of entries) {
    parts.push(header(e.path, e.data.size) as BlobPart)
    parts.push(e.data)
    const p = pad(e.data.size)
    if (p) parts.push(ZEROS.subarray(0, p) as BlobPart)
  }
  parts.push(ZEROS as BlobPart, ZEROS as BlobPart)   // the two all-zero blocks that end an archive
  return new Blob(parts, { type: 'application/x-tar' })
}

/**
 * gzip a Blob using the platform's CompressionStream — no dependency, no polyfill.
 *
 * The result is buffered back into a Blob rather than handed to `fetch` as a stream: a streaming
 * request body needs `duplex: 'half'`, which is Chromium-only. Buffering is fine here where the
 * synchronous build was not — `CompressionStream` pulls in chunks and yields between them, so the tab
 * stays responsive, and a Blob the browser decides is large is backed by disk rather than the heap.
 */
export async function gzip(data: Blob): Promise<Blob> {
  const stream = data.stream().pipeThrough(new CompressionStream('gzip'))
  return new Response(stream).blob()
}
