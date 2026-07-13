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

export type TarEntry = { path: string; data: Uint8Array }

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

/** Build an uncompressed tar of `entries`. Paths must be relative (no leading '/'). */
export function makeTar(entries: TarEntry[]): Uint8Array {
  const pad = (n: number) => (BLOCK - (n % BLOCK)) % BLOCK
  const total = entries.reduce((n, e) => n + BLOCK + e.data.length + pad(e.data.length), 0) + BLOCK * 2
  const out = new Uint8Array(total)
  let o = 0
  for (const e of entries) {
    out.set(header(e.path, e.data.length), o)
    o += BLOCK
    out.set(e.data, o)
    o += e.data.length + pad(e.data.length)
  }
  return out // trailing two zero blocks are already zero
}

/** gzip a byte array using the platform's CompressionStream — no dependency, no polyfill. */
export async function gzip(data: Uint8Array): Promise<Blob> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new CompressionStream('gzip'))
  return new Response(stream).blob()
}
