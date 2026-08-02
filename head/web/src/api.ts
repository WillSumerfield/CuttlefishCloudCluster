// The Head API, from the browser.
//
// Auth rides on the HttpOnly session cookie the Head sets at /login — the SPA never sees or stores the
// token (that's the point of HttpOnly: script can't read it, so an XSS can't steal it). Same origin as
// the API, so the cookie goes out automatically and we need no CORS and no Authorization header.
//
// Every route but /healthz is behind that session now, so ANY call can come back 401 — not just at
// login, but whenever a session expires mid-session. `unauthorized` is how the app notices and drops
// back to the login screen instead of rendering an empty board.

import { gzip, makeTar, type TarEntry } from './tar'
import { relPath, type Picked } from './project'

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
  name: string
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
export type Outcome = 'done' | 'failed' | 'cancelled' | 'retracted'

// A finished Spawn, as History serves it. Extends the live Spawn with its verdict and what it settled
// at. Served by GET /spawns/history — getHistory() above adapts that wire shape into this one.
export type ArchivedSpawn = Spawn & {
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
  // Recovery Window (docs/TODO.md): null/absent once it's closed (or was never made) — everything else
  // the Spawn wrote outside its declared outputs, held on the Head for 30 min. bytes rides alongside for
  // the same reason it does on RecoveryStatus: a size worth showing, not just a countdown. Optional (not
  // just nullable) so history/mock.ts's demo rows — predating this field — still typecheck as "none".
  recovery_bytes?: number | null
  recovery_expires_at?: string | null
  // Output retention (docs/TODO.md "Storage retention"): when this Spawn's DECLARED results are
  // deleted — 7 days after it ended, or sooner if the submitter's 8GB Allowance needed the room. The
  // longer, quieter sibling of the pair above, and the more consequential one: it is the results the
  // Spawn was submitted to produce. Null once they are gone, or before the tick has registered them.
  output_bytes?: number | null
  output_expires_at?: string | null
}

export type Me = { username: string; is_admin: boolean; must_change_password: boolean }

export type SpawnStatus = {
  id: number
  done: boolean
  state: string
  exit?: number
}

// Recovery Window (docs/TODO.md): a 30-min, 16GB-capped safety net for whatever a Spawn wrote OUTSIDE
// its declared $CCC_OUT — separate from (and never merged with) the permanent outputs tar. `expires_at`
// is an ISO timestamp; callers compute their own remaining-time countdown from it rather than a
// server-computed "seconds left" that would go stale the moment it's rendered.
export type RecoveryStatus = { bytes: number; expires_at: string }

// Output retention (docs/TODO.md "Storage retention"): the same two fields on the same terms, for the
// Spawn's DECLARED results — held 7 days from its end, and only while the submitter's 8GB Allowance has
// room. Identical shape to RecoveryStatus on purpose: the two panels render the same way, and a User
// learns one countdown rather than two.
export type RetentionStatus = { bytes: number; expires_at: string }

export class Unauthorized extends Error {}

// Every endpoint lives under /api so no API path collides with a client-side route the SPA owns (a hard
// reload on /karma used to hit the API and render raw JSON). Paths below stay bare; this prepends it.
const API = '/api'

// The message the whole SPA falls back to when the Head has no human reason to give: a 5xx (the crash
// detail is deliberately generic server-side), a non-JSON body (a proxy/gateway error page), or fetch
// rejecting outright (offline, DNS, TLS). Surfacing a bare "500" or the browser's "Failed to fetch"
// is what every catch below used to do; this reads as a sentence and says what to try.
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

const json = <T,>(path: string) => req(path).then((r) => r.json() as Promise<T>)

export const getArms = () => json<Arm[]>('/arms')
// The admin-curated catalog of Arm Features (`cuttle feature add`) — what the submit composer offers
// to require. Read-only from the Dashboard; mutating it is CLI+sudo only, like every other admin write.
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
  script: string
  node: string | null
  gpus: number
  minutes: number | null
  submitted_by: string | null
  cost: number | null
  recovery_bytes: number | null
  recovery_expires_at: string | null
  output_bytes: number | null
  output_expires_at: string | null
}

function outcomeOf(r: HistoryRow): Outcome {
  if (r.exit_code === null) return r.cancelled ? 'cancelled' : 'failed'
  return r.exit_code === 0 ? 'done' : 'failed'
}

// Finished Spawns, newest first. Design review runs on ?demo (see history/mock.ts).
export const getHistory = () =>
  json<HistoryRow[]>('/spawns/history').then((rows) =>
    rows.map(
      (r): ArchivedSpawn => ({
        id: r.job_id,
        name: r.script,
        state: r.cancelled ? 'cancelled' : 'finished',
        node: r.node,
        gpus: r.gpus,
        submitted_by: r.submitted_by,
        time_limit: r.minutes,
        outcome: outcomeOf(r),
        exit: r.exit_code,
        cost: r.cost,
        held: null, // the Head does not report actual held minutes yet, only the declared limit
        finished_at: r.finished_at ? Date.parse(r.finished_at) : Date.now(),
        recovery_bytes: r.recovery_bytes,
        recovery_expires_at: r.recovery_expires_at,
        output_bytes: r.output_bytes,
        output_expires_at: r.output_expires_at,
      }),
    ),
  )
export const getMe = () => json<Me>('/me')
export const getStatus = (id: number) => json<SpawnStatus>(`/spawns/${id}`)
// null = no open Recovery Window (never made, over the 16GB cap, evicted for the 50GB pool cap, or its
// 30 min are up) — a normal outcome for a finished Spawn, not surfaced as an error to the caller.
export async function getRecovery(id: number): Promise<RecoveryStatus | null> {
  try {
    return await json<RecoveryStatus>(`/spawns/${id}/recovery`)
  } catch (e) {
    if (e instanceof Unauthorized) throw e
    return null
  }
}
// null = nothing held (produced nothing, not yet registered by the tick, expired, or evicted for the
// Allowance) — a normal outcome, not an error, exactly like getRecovery above.
export async function getRetention(id: number): Promise<RetentionStatus | null> {
  try {
    return await json<RetentionStatus>(`/spawns/${id}/retention`)
  } catch (e) {
    if (e instanceof Unauthorized) throw e
    return null
  }
}
export const getLogs = (id: number) => req(`/spawns/${id}/logs`).then((r) => r.text())

/** End a live Spawn. Yours, or anyone's if you're an Admin (the Head 403s otherwise). */
export const cancelSpawn = (id: number) => req(`/spawns/${id}`, { method: 'DELETE' }).then(() => {})

// Arm lifecycle. `retract`/`return` are real, owner-scoped routes on the Head (app-layer — see
// retraction.py, no real Slurm DRAIN). `admit`/`benchmark`/`remove` are NOT and never will be: those
// stay Head-CLI-only forever (the Privileged Sidecar that would have exposed them here was killed as a
// roadmap goal), so their buttons on the roster only ever run under ?demo's mock.
const armPost = (name: string, verb: string) =>
  req(`/arms/${encodeURIComponent(name)}/${verb}`, { method: 'POST' }).then(() => {})

/** Owner-only: drain an Arm and stop its work immediately (3-min grace, then force-kill). */
export const retractArm = (name: string) => armPost(name, 'retract')
/** Owner-only: give a retracted Arm back — it never left the cluster, so no Admin, no new token. */
export const returnArm = (name: string) => armPost(name, 'return')
/** Admin, Head-CLI-only — this button only works under ?demo. */
export const admitArm = (name: string) => armPost(name, 'admit')
/** Admin, Head-CLI-only — this button only works under ?demo. */
export const benchmarkArm = (name: string) => armPost(name, 'benchmark')
/** Admin, Head-CLI-only — this button only works under ?demo. */
export const removeArm = (name: string) => req(`/arms/${encodeURIComponent(name)}`, { method: 'DELETE' }).then(() => {})

// A Recipe: a frozen, re-runnable snapshot of a project — the code plus how to run it. Private to its
// owner (the Head only ever serves you your own). It runs exactly what it captured until the owner hits
// Update code, so `frozen_at` is how stale that snapshot is. `size_bytes` is what it costs against the
// 24 GB per-User allowance. `updating` is true only while a fresh snapshot is uploading — the one moment
// a Recipe moves (the bloom); at rest it is still, because a frozen thing does not move.
export type Recipe = {
  name: string
  script: string          // entry point the snapshot runs
  args: string[]          // declared arguments, frozen with the code
  outputs: string[]       // declared output folders, frozen too — a Recipe with none could never be run
  gpus: number
  vram_mb: number
  minutes: number         // declared wall-clock — the same ask a Spawn carries
  size_bytes: number      // disk it holds against the allowance
  frozen_at: number       // epoch ms the code was last captured; Update code refreshes it
  updating: boolean       // a new snapshot is uploading right now → the bloom
  running: boolean        // a Spawn of this Recipe is live on the cluster right now → the passing cloud
  // Experiment tracking, frozen with the code. Read back (not just written) so Update code prefills what
  // the owner already answered instead of quietly resetting it to the defaults on every re-capture.
  wandb: boolean
  wandb_project: string
  wandb_entity: string
}

// Per-User storage allowance: 24 GB, a hard wall. Saving past it means deleting a Recipe first, never a
// warning. Mirrored in recipes/format.ts, where the meter reads from it — and on the Head, in
// recipes.py's MAX_USER_BYTES, which is the one that actually decides. The number lives in both places
// because the composer blocks a doomed save from File.size before spending the upload; the Head is still
// the authority, and answers 413 to anything that gets past.
export const RECIPE_ALLOWANCE_BYTES = 24 * 1024 ** 3

export const getRecipes = () => json<Recipe[]>('/recipes')

// Freeze a project into a new Recipe. Same wire shape as submit() — tar+gzip body, meta in the header —
// because a Recipe IS the same upload a Spawn carries, just persisted on the Head instead of run once. The
// Head weighs it against the 24 GB wall and answers 413 if it won't fit (the composer blocks first, from
// File.size, so the browser never spends the upload on a doomed save). Returns the frozen Recipe.
export type NewRecipe = {
  picked: Picked
  name: string
  script: string
  args: string[]
  outputs: string[]
  gpus: number
  vram_mb: number
  minutes: number
  // The Recipe's experiment-tracking config — the same three questions a Spawn answers, frozen with the
  // code rather than asked again on every run.
  wandb: boolean
  wandb_project: string
  wandb_entity: string
}

// The upload both save and update make: tar the picked folder, gzip it, put the settings in the header.
// Shared because an update IS a save — the Head releases the old snapshot's bytes and charges the new
// ones (delete-then-save), so the two differ only in which path they POST to.
async function recipeUpload(r: NewRecipe, path: string): Promise<Recipe> {
  const entries: TarEntry[] = []
  for (const f of r.picked.files) {
    entries.push({ path: relPath(f), data: new Uint8Array(await f.arrayBuffer()) })
  }
  const body = await gzip(makeTar(entries))
  const meta = {
    name: r.name, script: r.script, args: r.args, outputs: r.outputs, gpus: r.gpus,
    vram_mb: r.vram_mb, minutes: r.minutes,
    wandb: r.wandb, wandb_project: r.wandb_project, wandb_entity: r.wandb_entity,
  }
  const res = await req(path, {
    method: 'POST',
    headers: { 'X-CCC-Meta': JSON.stringify(meta), 'Content-Type': 'application/gzip' },
    body,
  })
  return res.json() as Promise<Recipe>
}

export const createRecipe = (r: NewRecipe) => recipeUpload(r, '/recipes')

/**
 * Re-capture the project into an existing Recipe — the one moment a frozen thing moves.
 *
 * Carries a body, unlike every other action here, because an update IS a fresh upload: there is no copy
 * of the old folder on your machine for the Head to diff against, and the Head deliberately keeps none
 * of a Spawn's code either. Explicit: until it lands, the Recipe keeps running the old snapshot, and a
 * save the 24 GB wall refuses leaves that old snapshot untouched.
 */
export const updateRecipe = (name: string, r: NewRecipe) =>
  recipeUpload(r, `/recipes/${encodeURIComponent(name)}/update`)

/**
 * Run the frozen snapshot — one click, from any machine, nothing to hand. Spawns a job (seen on Queue).
 * NOT WIRED UP YET: the Head answers 501. Saving, updating and deleting are all live; only this is not.
 */
export const runRecipe = (name: string) =>
  req(`/recipes/${encodeURIComponent(name)}/run`, { method: 'POST' }).then(() => {})
/** Delete a Recipe, freeing its bytes back to the allowance. */
export const deleteRecipe = (name: string) => req(`/recipes/${encodeURIComponent(name)}`, { method: 'DELETE' }).then(() => {})

// The W&B key the Head injects into every Spawn you submit (from either client). Held ENCRYPTED at rest —
// a third-party secret the Head keeps on your behalf, and unlike a password it must be recoverable in
// plaintext to be used, so it can't be hashed. ?demo runs these on local state (account/mock.ts).
export type WandbKeyStatus = { set: boolean; last4: string | null }

/** Masked status only — whether a key is stored, and its last 4 chars. Never the plaintext. */
export const getWandbKey = () => json<WandbKeyStatus>('/account/wandb')
/** The plaintext, for the deliberate Reveal. A separate route so the masked read never carries the secret. */
export const revealWandbKey = () => json<{ key: string }>('/account/wandb/reveal').then((r) => r.key)
/** Store (or replace) the key. */
export const setWandbKey = (key: string) =>
  req('/account/wandb', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  }).then(() => {})
/** Clear the key — the Head stops injecting it. */
export const deleteWandbKey = () => req('/account/wandb', { method: 'DELETE' }).then(() => {})

// The Tide, as the Head serves it: Demand per Arm over CLOCK time — how badly each card has been wanted,
// hour by hour, over the last `days`. This is the ONLY surface the Tide is seen through: not a forecast,
// not a quote. The frontend folds this into the hour-of-day shape itself (tide/fold.ts). A cold or
// history-less cluster returns [], which reads as a flat, honest 1.0 Tide; ?demo runs the mock series.
export type DemandSample = { node: string; at: string; demand: number }
export const getDemand = (days = 30) => json<DemandSample[]>(`/karma/demand?days=${days}`)

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

/** Download a Spawn's outputs tar, straight to the user's disk. */
export async function downloadOutputs(id: number): Promise<void> {
  const blob = await req(`/spawns/${id}/outputs`).then((r) => r.blob())
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `spawn-${id}-outputs.tar.gz`
  a.click()
  URL.revokeObjectURL(url)
}

/** Download a Spawn's Recovery Window copy — everything it wrote OUTSIDE $CCC_OUT, while the window is
 *  still open. Same shape as downloadOutputs, distinct route and filename (docs/TODO.md: never merge
 *  the two into one artifact whose contents would silently change once the window closes). */
export async function downloadRecovery(id: number): Promise<void> {
  const blob = await req(`/spawns/${id}/recovery/download`).then((r) => r.blob())
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `spawn-${id}-recovery.tar.gz`
  a.click()
  URL.revokeObjectURL(url)
}

export type SubmitOpts = {
  picked: Picked
  name: string | null // optional label for the run; the Head falls back to the script name when unset
  script: string
  gpu: number
  // Wall-clock minutes, and GPU memory in GB. Both required by the Head — a Spawn with no declared
  // length can't be priced, and one with no declared need can't be placed.
  minutes: number
  vram: number
  node: string | null // admin-only; the Head 403s it for everyone else
  args: string[]
  // Log this Spawn to W&B at all. A stored key is otherwise used for EVERY Spawn, and "not this one"
  // has to be sayable — but only as intent: we send a boolean, never WANDB_MODE, so the Head stays the
  // one composing the environment. False disables it there even with a key stored.
  wandb: boolean
  // Which W&B run this Spawn is — the only part of the W&B environment a client gets to decide (the
  // Head holds the key). Blank means "unset": the project falls back to the Head's `ccc` default, the
  // entity to the W&B account's own. Ignored by the Head when `wandb` is false.
  wandbProject: string
  wandbEntity: string
  // Subdirs made under the one $CCC_OUT root before the payload runs (docs/TODO.md's now-closed "Spawn
  // Output Enforcement") — required, non-empty; the Head 422s a Spawn that declares none.
  outputs: string[]
  // Arm Features (docs/TODO.md) this Spawn requires, picked from the Head's catalog — optional, and
  // most Spawns declare none. An unsatisfiable request (no Arm has all of them) is a 422, not a queue.
  features: string[]
  // Which dependency file builds this Spawn's Python environment (docs/TODO.md "Python Environments"),
  // relative to the project root. Blank = let the Head look in the root itself; 'none' = build nothing.
  manifest: string
}

/** What the Head decided this Spawn's environment is, echoed back at submit. */
export type SubmitResult = { id: number; environment: string | null }

/**
 * Tar + gzip the picked files and POST them. This is the ONLY point at which file contents are read —
 * the preview the user confirmed was built from File.size alone, which costs no I/O. Same wire format
 * as the CLI (gzip-tar body + X-CCC-Meta header), so the server has exactly one upload path.
 */
export async function submit({
  picked, name, script, gpu, minutes, vram, node, args, wandb, wandbProject, wandbEntity, outputs,
  features, manifest,
}: SubmitOpts): Promise<SubmitResult> {
  const entries: TarEntry[] = []
  for (const f of picked.files) {
    entries.push({ path: relPath(f), data: new Uint8Array(await f.arrayBuffer()) })
  }
  const body = await gzip(makeTar(entries))
  // The Head owns the key, the client owns the run: we send WANDB_PROJECT/WANDB_ENTITY and nothing else
  // — never a key (the browser has none), never WANDB_MODE (this used to hardcode `disabled`, which
  // silently cost every Dashboard Spawn its metrics). The Head fills in the key it holds for you.
  //
  // A blank field is OMITTED rather than sent empty. WANDB_ENTITY="" breaks wandb.init() outright
  // instead of falling back to the account default, and a blank project is the Head's `ccc` default —
  // which lives there alone, so the placeholder in the composer only ever displays it.
  //
  // `wandb` is the off-switch and travels OUTSIDE wandb_env for that reason: inside it, it would have
  // to be a WANDB_MODE, and a client-sent mode is exactly what the Head stopped honouring. The Head
  // reads an absent flag as true, so sending it always is a statement, never a requirement.
  const wandb_env: Record<string, string> = {}
  if (wandbProject.trim()) wandb_env.WANDB_PROJECT = wandbProject.trim()
  if (wandbEntity.trim()) wandb_env.WANDB_ENTITY = wandbEntity.trim()
  // Omitted rather than sent empty, matching wandb_env's fields above — most Spawns require none, and
  // the Head reads an absent key the same as an empty list.
  const meta = {
    name, script, gpu, minutes, vram, node, args, wandb, wandb_env, outputs,
    ...(features.length ? { features } : {}),
    // Omitted when blank, like the fields above: absent means "look for a dependency file yourself",
    // which is not the same instruction as any string this field could hold.
    ...(manifest.trim() ? { manifest: manifest.trim() } : {}),
  }
  const r = await req('/submit', {
    method: 'POST',
    headers: { 'X-CCC-Meta': JSON.stringify(meta), 'Content-Type': 'application/gzip' },
    body,
  })
  const body_ = await r.json()
  return { id: body_.id, environment: body_.environment ?? null }
}

// A Tide Pool (docs/TODO.md): request one from the Dashboard, same queue/Eligibility/Escrow as any
// Spawn — but there is no script to name, so this is deliberately NOT `submit` with a few fields
// blanked out. A project folder is optional (unlike Script mode, where it's required): picking one just
// lands it at $proj on the Arm so the live shell has it to hand — nothing in it is ever run
// automatically. Actual shell access is CLI-only; the Dashboard's part ends the moment the Spawn appears
// (it shows up in the queue like anything else — see QueuePage).
export type SubmitTidepoolOpts = {
  minutes: number
  vram: number
  gpu: number
  node: string | null
  features: string[]
  picked: Picked | null
  name: string | null
}

export async function submitTidepool({
  minutes, vram, gpu, node, features, picked, name,
}: SubmitTidepoolOpts): Promise<number> {
  // A genuinely empty gzip-tar when no project was picked, not an empty body — the Head always opens
  // the upload as a tar archive (ccc_head_api.spawns.submit), whether or not there is a project behind
  // it. `makeTar([])` writes the two all-zero end-of-archive blocks tar readers expect, the same bytes
  // Python's tarfile writes for no members.
  const entries: TarEntry[] = []
  for (const f of picked?.files ?? []) {
    entries.push({ path: relPath(f), data: new Uint8Array(await f.arrayBuffer()) })
  }
  const body = await gzip(makeTar(entries))
  const meta = {
    minutes, vram, gpu, node, tidepool: true, outputs: [], name,
    ...(features.length ? { features } : {}),
  }
  const r = await req('/submit', {
    method: 'POST',
    headers: { 'X-CCC-Meta': JSON.stringify(meta), 'Content-Type': 'application/gzip' },
    body,
  })
  return (await r.json()).id
}
