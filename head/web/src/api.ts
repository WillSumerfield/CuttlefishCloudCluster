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
  // benchmark Spawn at enroll) and how much it can hold. Null until an Arm has been benchmarked.
  gpu: string | null
  vram_mb: number | null
  power: number | null
  // Arm Features (docs/TODO.md) this Arm declared at enroll — heavy/hardware-licence-gated software
  // (VSim, IsaacSim, ...), each with the local path it's installed at. Empty for most Arms.
  features: { name: string; path: string }[]
}

// `submitted_by` is Attribution: who actually submitted the Spawn. Slurm's own owner field says the
// Mantle (`ccc`) for every Spawn — all Spawns run as one identity — so it is useless here and we don't
// even surface it.
export type Spawn = {
  id: number
  state: string
  node: string | null
  gpus: number
  submitted_by: string | null
  // Declared duration (`--time`), in minutes. Null on Spawns submitted before it was surfaced.
  time_limit: number | null
}

// How a Spawn ended. `retracted` = its Arm was reclaimed (Owner hit Retract); it settles at zero and
// carries no exit code. `failed` carries the process exit code. done/cancelled are the quiet outcomes;
// failed/retracted are the loud ones (drawn in --alarm) — see temp/DESIGN.md §State colour.
//
// `settling` is not an ending at all: it is the ≤30s in which a Spawn has left the queue and the karma
// tick has not yet stamped what it did (see outcomeOf). It exists so that gap has a word of its own —
// the alternative is reading an absent exit code as a failure, which is what it used to do.
export type Outcome = 'done' | 'failed' | 'cancelled' | 'retracted' | 'settling'

// A finished Spawn, as History serves it. Extends the live Spawn with its verdict and what it settled
// at. Served by GET /spawns/history — getHistory() above adapts that wire shape into this one.
export type ArchivedSpawn = Spawn & {
  // The entry script — what RAN, and what titles the row.
  script: string
  outcome: Outcome
  // Process exit code. Present on done (0) and failed (non-zero); null on cancelled/retracted, which
  // never ran to an exit.
  exit: number | null
  // Karma this Spawn settled at (Ph4). null before it was priced; 0 for a retracted Spawn (killing it
  // costs the submitter nothing). Plain ink, never pigment — pigment means standing, not cost.
  cost: number | null
  // Minutes actually HELD (what Karma meters), distinct from the declared time_limit. null if unknown.
  held: number | null
  // Epoch ms it finished — the sort key. History is one flat stream, newest first.
  finished_at: number
  // Null once gone, or before the tick has registered them.
}

export type Me = { username: string; is_admin: boolean; must_change_password: boolean }

// One Spawn, whole — GET /spawns/{id}. `state`/`done`/`exit` are the live verdict (read from squeue and
// the exit file); everything below them is the archive row that rides along, and is absent only for a
// Spawn the API never submitted (the enrollment benchmark). Deliberately indifferent to whether its
// subject is queued, running or a year old: the Spawn page renders from this one fetch either way.
export type SpawnDetail = {
  id: number
  done: boolean
  state: string
  exit?: number
  submitted_at?: string
  finished_at?: string | null
  exit_code?: number | null
  /** What RAN. */
  script?: string
  gpus?: number | null
  vram_gb?: number | null
  minutes?: number | null
  /** Demanded (Admin `--node`), not where it ran — see `placed_node`. */
  node?: string | null
  /** Where the cluster actually put it, recorded by the meter. Null while it is still queued, and for
   *  a Spawn that ended before a single meter pass caught it running. */
  placed_node?: string | null
  submitted_by?: string | null
  // What it settled at. Null while it still holds an Escrow, and for a Spawn that never settled at all
  // (Admin-owned, or ended before the meter reached it).
  cost?: number | null
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
// The Arm Features CCC supports — what the submit composer offers to require. Built into CCC, not
// curated, so there is nothing to mutate from anywhere: it reports what this HEAD's version knows.
// What the cluster can currently PLACE is narrower (an Arm has to carry it); that is checked at submit.
export const getFeatureCatalog = () => json<string[]>('/features')
export const getSpawns = () => json<Spawn[]>('/spawns')

// The wire shape GET /spawns/history actually answers with (see ccc_head_api.main.spawns_history) —
// job_id/script/exit_code/cancelled, an ISO finished_at — which is not ArchivedSpawn's shape below.
// Mirrors the CLI's own read of the same rows (cuttle/spawns.py's _history_outcome).
type HistoryRow = {
  job_id: number
  submitted_at: string
  finished_at: string | null
  exit_code: number | null
  cancelled: boolean
  // The entry script (what RAN).
  script: string
  // What the submitter DEMANDED (Admin-only `--node`), which is null for almost every Spawn — never
  // where it ran. `placed_node` is that, recorded by the meter; null when the Spawn ended too fast to
  // be metered at all, or predates the column.
  node: string | null
  placed_node: string | null
  gpus: number
  minutes: number | null
  submitted_by: string | null
  cost: number | null
}

// The `finished_at` guard is FIRST, and it is the whole reason this function is not two lines. A Spawn
// leaves squeue the moment it ends, and `/spawns/history` excludes only what is still live — so it lands
// in the archive immediately, while its outcome is stamped by the karma tick up to TICK_SECONDS (30s)
// later. db.mark_finished writes `exit_code` and `finished_at` TOGETHER, so an unstamped row is exactly
// "ended, not yet settled" and has a null exit code for a reason that has nothing to do with failing.
//
// Without this guard that row read `failed`, in --alarm red, for a Spawn that had just exited 0.
// Observed on hardware 2026-08-15 (Spawn 5): History does not poll, so whoever had the page open when a
// Spawn finished kept the red verdict until they reloaded — hours, in that case. The CLI got this right
// all along (cuttle/spawns.py's _history_outcome checks finished_at first); the comment above this type
// claimed to mirror it and did not.
function outcomeOf(r: HistoryRow): Outcome {
  if (r.finished_at === null) return 'settling'
  if (r.exit_code === null) return r.cancelled ? 'cancelled' : 'failed'
  return r.exit_code === 0 ? 'done' : 'failed'
}

// Finished Spawns, newest first.
export const getHistory = () =>
  json<HistoryRow[]>('/spawns/history').then((rows) =>
    rows.map(
      (r): ArchivedSpawn => ({
        id: r.job_id,
        script: r.script,
        state: r.cancelled ? 'cancelled' : 'finished',
        // For a FINISHED Spawn "the arm" means the one it ran on. The declared node is the fallback
        // only because when it is set at all, an Admin named it and the Spawn went there.
        node: r.placed_node ?? r.node,
        gpus: r.gpus,
        submitted_by: r.submitted_by,
        time_limit: r.minutes,
        outcome: outcomeOf(r),
        exit: r.exit_code,
        cost: r.cost,
        held: null, // the Head does not report actual held minutes yet, only the declared limit
        finished_at: r.finished_at ? Date.parse(r.finished_at) : Date.now(),
      }),
    ),
  )
export const getMe = () => json<Me>('/me')
export const getSpawn = (id: number) => json<SpawnDetail>(`/spawns/${id}`)
/** A Spawn's stderr — where a crash's traceback lands. `getLogs` serves stdout, which is empty on one. */
export const getStderr = (id: number) => req(`/spawns/${id}/stderr`).then((r) => r.text())
/** The Environment build's output — empty string for a Spawn that declared none. The Head's work, not
 *  the Spawn's, which is why it is its own stream rather than the head of stdout. */
export const getLogs = (id: number) => req(`/spawns/${id}/logs`).then((r) => r.text())

/** End a live Spawn. Yours, or anyone's if you're an Admin (the Head 403s otherwise). */
export const cancelSpawn = (id: number) => req(`/spawns/${id}`, { method: 'DELETE' }).then(() => {})

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

// One claim holding part of a User's Escrow — a Spawn that has not settled yet. `amount` is the ratchet
// (the greater of the hold quoted at submit and what the meter has accrued since), so it only ever
// climbs while the Spawn runs. The claims sum to the `escrow` on the same Standing.
export type EscrowClaim = { job_id: number; script: string | null; node: string | null; amount: number }

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
  escrow_spawns: EscrowClaim[]
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


export type SubmitOpts = {
  script: string
  gpu: number
  // Wall-clock minutes, and GPU memory in GB. Both required by the Head — a Spawn with no declared
  // length can't be priced, and one with no declared need can't be placed.
  minutes: number
  vram: number
  node: string | null // admin-only; the Head 403s it for everyone else
  args: string[]
  // Arm Features (docs/TODO.md) this Spawn requires, picked from the Head's catalog — optional, and
  // most Spawns declare none. An unsatisfiable request (no Arm has all of them) is a 422, not a queue.
  features: string[]
}

const postSubmit = async (meta: object): Promise<number> => {
  const r = await req('/submit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(meta),
  })
  return (await r.json()).id
}

export function submit({
  script, gpu, minutes, vram, node, args, features,
}: SubmitOpts): Promise<number> {
  // `features` is omitted rather than sent empty — most Spawns require none, and the Head reads an
  // absent key the same as an empty list.
  return postSubmit({
    script, gpu, minutes, vram, node, args,
    ...(features.length ? { features } : {}),
  })
}

// A Tide Pool (docs/TODO.md): request one from the Dashboard, same queue/Eligibility/Escrow as any Spawn
// — but no script to name, so deliberately NOT `submit` with fields blanked out. Shell access is
// CLI-only; Dashboard's part ends once the Spawn appears in the queue.
export type SubmitTidepoolOpts = {
  minutes: number
  vram: number
  gpu: number
  node: string | null
  features: string[]
}

export function submitTidepool({
  minutes, vram, gpu, node, features,
}: SubmitTidepoolOpts): Promise<number> {
  return postSubmit({
    minutes, vram, gpu, node, tidepool: true,
    ...(features.length ? { features } : {}),
  })
}
