// The Head API, from the browser.
//
// Auth rides on the HttpOnly session cookie the Head sets at /login — the SPA never sees or stores the
// token (script can't read HttpOnly, so XSS can't steal it). Same origin as the API, so the cookie goes
// out automatically: no CORS, no Authorization header.
//
// Every route but /healthz is behind that session, so ANY call can come back 401, not just at login.
// `Unauthorized` is how the app notices and drops back to the login screen.

// `owner` is the User who donated the machine — named by the Admin when they authorized it, never
// declared by the donor. Karma will pay its passive income here.
export type Arm = {
  name: string
  state: string
  gpus: number
  owner: string | null
  mesh_ip: string | null
  gpu_device: string | null
  admitted: boolean
  // App-layer only — there is no real Slurm DRAIN behind this (see the Head's retraction.py). The
  // owner paused it; `state` (raw sinfo) never reflects that, so it rides its own field rather than a
  // synthetic word squeezed into `state`.
  retracted: boolean
  // The hardware profile Karma prices from: what the card can do (Power, in TFLOP/s, measured by a
  // benchmark Dive at enroll) and how much it can hold. Null until an Arm has been benchmarked.
  gpu: string | null
  vram_mb: number | null
  power: number | null
  // Arm Features (docs/TODO.md) this Arm declared at enroll — heavy/hardware-licence-gated software
  // (VSim, IsaacSim, ...), each with the local path it's installed at. Empty for most Arms.
  features: { name: string; path: string }[]
}

// One row of the live queue. `started_by` is who started it — Slurm's own owner field names a Guise,
// so it is never surfaced.
export type Dive = {
  id: number
  state: string
  node: string | null
  gpus: number
  started_by: string | null
  // Declared duration (`--time`), in minutes.
  time_limit: number | null
  /** The Dive's main folder, from the Tidepool home. */
  main?: string | null
}

// How a Dive ended (dive.outcome on the Head). `ended` = someone ended it; `timed out` = it ran its
// --time; `lost` = its job died first (the Arm dropped, the node rebooted) — the one loud outcome.
// `settling` is the ≤30s after it left the queue before the tick stamps the end (outcome null).
export type Outcome = 'ended' | 'timed out' | 'lost' | 'settling'

// A finished Dive, as History shows it.
export type ArchivedDive = Dive & {
  outcome: Outcome
  // Karma it settled at. null before it was priced (or never: an Admin's). Plain ink, never pigment.
  cost: number | null
  // Minutes actually held, distinct from the declared time_limit. null if unknown.
  held: number | null
  // Epoch ms it finished — the sort key. History is one flat stream, newest first.
  finished_at: number
}

export type Me = { username: string; is_admin: boolean; must_change_password: boolean }

// One Dive, whole — GET /dives/{id}: state + the archive row, whether it is queued, running or a year
// old. `state` is Slurm's while it is in the queue (pending, running, …), `ending` once it has left
// and not been stamped, then its outcome. The connection fields are present only for its owner.
export type DiveDetail = {
  id: number
  done: boolean
  state: string
  submitted_at: string
  finished_at: string | null
  gpus: number | null
  vram_gb: number | null
  minutes: number | null
  /** Where the cluster put it, recorded by the meter. Null while queued. */
  placed_node: string | null
  started_by: string | null
  main: string
  deps: string[]
  // Null while it still holds an Escrow, and for one that never settled at all (Admin-owned).
  cost: number | null
  outcome: Outcome | null
  // Owner only: where `cuttle dive attach` connects, once an sshd answers (`ready`).
  ready?: boolean
  node?: string | null
  alias?: string
}

export class Unauthorized extends Error {}

// Every endpoint lives under /api so no API path collides with a client-side route the SPA owns.
// Paths below stay bare; this prepends it.
const API = '/api'

// Fallback message when the Head has no human reason to give: a 5xx (detail deliberately generic
// server-side), a non-JSON body (proxy/gateway error page), or fetch rejecting outright (offline/DNS/TLS).
// Reads as a sentence and says what to try, rather than a bare "500" or "Failed to fetch".
const HEAD_UNREACHABLE = "Couldn't reach the Head — check your connection and try again."
const HEAD_ERROR = "The Head hit an error handling that request — try again in a moment."

async function req(path: string, init?: RequestInit): Promise<Response> {
  let r: Response
  try {
    r = await fetch(API + path, init)
  } catch {
    throw new Error(HEAD_UNREACHABLE) // fetch rejected — never reached the Head; no status to read.
  }
  if (r.ok) return r
  let detail: string | null = null
  try {
    detail = (await r.json()).detail ?? null
  } catch {
    /* not JSON */
  }
  // 401 means the SESSION is bad — and only that, so the app can safely read it as "bounce to the
  // login screen". Never invent the message: a route that refuses an authed caller for its own
  // reasons (a wrong current password, say) answers 403, and its detail must reach the user intact.
  if (r.status === 401) throw new Unauthorized(detail ?? 'your session ended')
  // A 4xx with a real `detail` is the Head's own human reason (a policy violation, a bad name) and
  // reaches the user verbatim. A 5xx, or any response with no JSON detail, has nothing worth showing
  // raw — frame it rather than leaking a bare status number.
  if (detail !== null && r.status < 500) throw new Error(detail)
  throw new Error(HEAD_ERROR)
}

/** The user-facing message from a caught API error. Everything `req` throws is an `Error` whose
 *  message is already a sentence (or `Unauthorized`, which callers handle before reaching here), so a
 *  catch can surface this directly instead of re-deriving `e instanceof Error ? e.message : ...`. */
export const errMessage = (e: unknown): string => (e instanceof Error ? e.message : String(e))

// `init` for the handful of non-GETs whose REPLY is the point (a mint answers the public key it
// just made). Absent for every read, which is nearly all of them.
const json = <T,>(path: string, init?: RequestInit) =>
  req(path, init).then((r) => r.json() as Promise<T>)

export const getArms = () => json<Arm[]>('/arms')
export const getDives = () => json<Dive[]>('/dives')

// The wire shape GET /dives/history answers with (ccc_head_api.dive.archive) — adapted below into
// ArchivedDive.
type HistoryRow = {
  job_id: number
  submitted_at: string
  finished_at: string | null
  placed_node: string | null
  gpus: number
  minutes: number | null
  started_by: string | null
  main: string
  cost: number | null
  outcome: Exclude<Outcome, 'settling'> | null
}

// Finished Dives, newest first. A null outcome is a Dive that left the queue moments ago and has not
// been stamped yet — `settling`, never read as a failure.
export const getHistory = () =>
  json<HistoryRow[]>('/dives/history').then((rows) =>
    rows.map(
      (r): ArchivedDive => ({
        id: r.job_id,
        main: r.main,
        state: r.outcome ?? 'settling',
        node: r.placed_node,
        gpus: r.gpus,
        started_by: r.started_by,
        time_limit: r.minutes,
        outcome: r.outcome ?? 'settling',
        cost: r.cost,
        held: null, // the Head does not report actual held minutes yet, only the declared limit
        finished_at: r.finished_at ? Date.parse(r.finished_at) : Date.now(),
      }),
    ),
  )
export const getMe = () => json<Me>('/me')
export const getDive = (id: number) => json<DiveDetail>(`/dives/${id}`)

/** End your own live Dive (the Head 403s anyone else's). */
export const endDive = (id: number) => req(`/dives/${id}`, { method: 'DELETE' }).then(() => {})

// Arm lifecycle, in full. `retract`/`return` are real, owner-scoped routes on the Head (app-layer — see
// retraction.py, no real Slurm DRAIN). There is no admit/benchmark/remove here and never will be: those
// stay Head-CLI-only forever (the Privileged Sidecar that would have exposed them was killed as a
// roadmap goal), so the roster offers no button for them rather than one that always fails.
const armPost = (name: string, verb: string) =>
  req(`/arms/${encodeURIComponent(name)}/${verb}`, { method: 'POST' }).then(() => {})

/** Owner-only: drain an Arm and stop its work immediately (3-min grace, then force-kill). */
export const retractArm = (name: string) => armPost(name, 'retract')
/** Owner-only: give a retracted Arm back — it never left the cluster, so no Admin, no new token. */
export const returnArm = (name: string) => armPost(name, 'return')

// The Tide, as the Head serves it: Demand per Arm over CLOCK time — how badly each card has been wanted,
// hour by hour, over the last `days`. This is the ONLY surface the Tide is seen through: not a forecast,
// not a quote. The frontend folds this into the hour-of-day shape itself (tide/fold.ts). A cold or
// history-less cluster returns [], which reads as a flat, honest 1.0 Tide.
export type DemandSample = { node: string; at: string; demand: number }
export const getDemand = (days = 30) => json<DemandSample[]>(`/karma/demand?days=${days}`)

// One claim holding part of a User's Escrow — a Dive that has not settled yet. `amount` is the ratchet
// (the greater of the hold quoted at start and what the meter has accrued since), so it only ever
// climbs while the Dive runs. The claims sum to the `escrow` on the same Standing.
export type EscrowClaim = { job_id: number; main: string | null; node: string | null; amount: number }

// One User's standing, as GET /karma serves it — everyone's, always, because a Balance is meaningless in
// isolation: Karma ranks Users against each other. Admins never appear (they are outside the economy).
// `share` is the saturating [0,1] effect the Balance actually has; `nice` is what Slurm is told.
export type Standing = {
  user_id: number
  username: string
  balance: number
  escrow: number
  share: number
  nice: number
  escrow_dives: EscrowClaim[]
}
export const getStandings = () => json<Standing[]>('/karma')

// A real event in your ledger — a Settlement or an Admin's Override. `amount` is signed from YOUR point
// of view: + earned, − paid. A null `counterparty` is the point, not a missing name: no source means
// Karma was conjured (an Override), no destination means it was destroyed (a burn on a commons Arm).
//
// No `kind: 'decay'`: decay is the sag of `curve` BETWEEN events, not an event itself — the curve ships
// alongside the rows rather than being derived from them, and there is no stored Balance to decay.
export type LedgerRow = {
  id: number
  at: string
  kind: 'settle' | 'override'
  amount: number
  job_id: number | null
  note: string | null
  counterparty: string | null
}
export type Ledger = { rows: LedgerRow[]; curve: { at: string; balance: number }[] }
export const getLedger = (days = 30) => json<Ledger>(`/karma/ledger?days=${days}`)

// Who spent the cluster: daily Karma spend per User, heaviest first. `series` is `days` UTC-day totals,
// oldest first — every User's array is the same length and indexes to the same day, so they stack. A User
// who spent nothing in the window is absent rather than a row of zeroes. `balance` rides along because
// the bands wear pigment, and pigment means standing.
export type UsageUser = { user_id: number; username: string; balance: number; total: number; series: number[] }
export const getUsage = (days = 30) => json<UsageUser[]>(`/karma/usage?days=${days}`)

export async function login(username: string, password: string): Promise<Me> {
  let r: Response
  try {
    r = await fetch(API + '/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
  } catch {
    throw new Error("Couldn't reach the sign-in service — check your connection and try again.")
  }
  if (r.status === 401) throw new Error('Incorrect username or password.')
  if (r.status === 429) {
    let detail: string | null = null
    try { detail = (await r.json()).detail ?? null } catch { /* not JSON */ }
    throw new Error(detail ?? 'Too many failed attempts — try again later.')
  }
  if (!r.ok) throw new Error("The sign-in service isn't responding right now — try again in a moment.")
  return r.json()
}

export async function logout(): Promise<void> {
  await fetch(API + '/logout', { method: 'POST' })
}

export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  await req('/passwd', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  })
}

// The caller's own Tidepool (always theirs — no route takes a username). `used_gb` is 0 while the
// image is not mounted (`mounted` false); `size_gb` is null on a Head set up before Tidepools.
export type Tidepool = {
  state: string
  used_gb: number
  mounted: boolean
  size_gb: number | null
  port: number | null
  user: string
  host_key: string | null
  alias: string
}
export const getTidepool = () => json<Tidepool>('/tidepool')
export const tidepoolAct = (verb: 'restart' | 'reset') =>
  req(`/tidepool/${verb}`, { method: 'POST' }).then(() => {})

export type SshKey = { id: number; name: string | null; pubkey: string; added_at: string }
export const getSshKeys = () => json<SshKey[]>('/ssh-keys')
export const addSshKey = (pubkey: string, name?: string) =>
  req('/ssh-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pubkey, name: name || null }),
  }).then(() => {})
export const removeSshKey = (id: number) => req(`/ssh-keys/${id}`, { method: 'DELETE' }).then(() => {})

// The file browser over your own Tidepool home (paths from the home; "." is the home itself).
export type FileEntry = {
  name: string
  path: string
  type: 'dir' | 'file' | 'link' | 'other'
  size: number | null
  mtime: number
  /** A main folder a Dive holds: read-only until it ends. */
  locked: boolean
}
export const listFiles = (path: string) =>
  json<{ path: string; entries: FileEntry[]; locked: string | null }>(`/files?path=${encodeURIComponent(path)}`)
export const downloadUrl = (path: string) => `${API}/files/download?path=${encodeURIComponent(path)}`
const jsonPost = (path: string, body: unknown) =>
  req(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(() => {})
export const makeFolder = (path: string) => jsonPost('/files/mkdir', { path })
export const renameFile = (path: string, to: string) => jsonPost('/files/rename', { path, to })
export const deleteFile = (path: string) =>
  req(`/files?path=${encodeURIComponent(path)}`, { method: 'DELETE' }).then(() => {})

/** One file's upload. `'exists'` = a 409 (already there): the caller asks before overwriting. */
export async function uploadFile(path: string, body: Blob, overwrite: boolean): Promise<'ok' | 'exists'> {
  let r: Response
  try {
    r = await fetch(`${API}/files?path=${encodeURIComponent(path)}&overwrite=${overwrite}`,
      { method: 'PUT', body })
  } catch {
    throw new Error(HEAD_UNREACHABLE)
  }
  if (r.status === 409 && !overwrite) return 'exists'
  if (r.ok) return 'ok'
  if (r.status === 401) throw new Unauthorized('your session ended')
  let detail: string | null = null
  try { detail = (await r.json()).detail ?? null } catch { /* not JSON */ }
  throw new Error(detail ?? HEAD_ERROR)
}
