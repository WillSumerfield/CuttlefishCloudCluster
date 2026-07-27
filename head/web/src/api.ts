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
// at. There is NO live endpoint for this yet — getSpawns() serves pending+running only — so History runs
// on ?demo mock until the Head grows a /history route.
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
}

export type Me = { username: string; is_admin: boolean; must_change_password: boolean }

export type SpawnStatus = {
  id: number
  done: boolean
  state: string
  exit?: number
}

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
export const getSpawns = () => json<Spawn[]>('/spawns')
// Finished Spawns, newest first. The Head does not serve this yet; live, History shows its empty state
// until it does. Design review runs on ?demo (see history/mock.ts).
export const getHistory = () => json<ArchivedSpawn[]>('/history')
export const getMe = () => json<Me>('/me')
export const getStatus = (id: number) => json<SpawnStatus>(`/spawns/${id}`)
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
// 20 GB per-User allowance. `updating` is true only while a fresh snapshot is uploading — the one moment
// a Recipe moves (the bloom); at rest it is still, because a frozen thing does not move.
export type Recipe = {
  name: string
  script: string          // entry point the snapshot runs
  args: string[]          // declared arguments, frozen with the code
  gpus: number
  vram_mb: number
  minutes: number         // declared wall-clock — the same ask a Spawn carries
  size_bytes: number      // disk it holds against the allowance
  frozen_at: number       // epoch ms the code was last captured; Update code refreshes it
  updating: boolean       // a new snapshot is uploading right now → the bloom
  running: boolean        // a Spawn of this Recipe is live on the cluster right now → the passing cloud
}

// Per-User storage allowance: 20 GB, a hard wall. Saving past it means deleting a Recipe first, never a
// warning. Mirrored in recipes/format.ts, where the meter reads from it.
export const RECIPE_ALLOWANCE_BYTES = 20 * 1024 ** 3

export const getRecipes = () => json<Recipe[]>('/recipes')

// Freeze a project into a new Recipe. Same wire shape as submit() — tar+gzip body, meta in the header —
// because a Recipe IS the same upload a Spawn carries, just persisted on the Head instead of run once. The
// Head weighs it against the 20 GB wall and answers 413 if it won't fit (the composer blocks first, from
// File.size, so the browser never spends the upload on a doomed save). Returns the frozen Recipe.
export type NewRecipe = {
  picked: Picked
  name: string
  script: string
  args: string[]
  gpus: number
  vram_mb: number
  minutes: number
}

export async function createRecipe(r: NewRecipe): Promise<Recipe> {
  const entries: TarEntry[] = []
  for (const f of r.picked.files) {
    entries.push({ path: relPath(f), data: new Uint8Array(await f.arrayBuffer()) })
  }
  const body = await gzip(makeTar(entries))
  const meta = { name: r.name, script: r.script, args: r.args, gpus: r.gpus, vram_mb: r.vram_mb, minutes: r.minutes }
  const res = await req('/recipes', {
    method: 'POST',
    headers: { 'X-CCC-Meta': JSON.stringify(meta), 'Content-Type': 'application/gzip' },
    body,
  })
  return res.json() as Promise<Recipe>
}

// Recipe actions. Like the Arm lifecycle, the Head serves none of these yet — the page runs them on
// ?demo mock (recipes/mock.ts), mutating local state; live, they hit these paths.
const recipePost = (name: string, verb: string) =>
  req(`/recipes/${encodeURIComponent(name)}/${verb}`, { method: 'POST' }).then(() => {})

/** Run the frozen snapshot — one click, from any machine, nothing to hand. Spawns a job (seen on Queue). */
export const runRecipe = (name: string) => recipePost(name, 'run')
/** Re-capture the project into the Recipe. Explicit: until it lands, the Recipe keeps running the old code. */
export const updateRecipe = (name: string) => recipePost(name, 'update')
/** Delete a Recipe, freeing its bytes back to the allowance. */
export const deleteRecipe = (name: string) => req(`/recipes/${encodeURIComponent(name)}`, { method: 'DELETE' }).then(() => {})

// The W&B key the Head injects into every Spawn you submit (from either client). Held ENCRYPTED at rest —
// a third-party secret the Head keeps on your behalf, and unlike a password it must be recoverable in
// plaintext to be used, so it can't be hashed. The Head serves none of these routes yet, so live they 404
// and the Account module shows "not available yet"; ?demo runs them on local state (account/mock.ts).
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
}

/**
 * Tar + gzip the picked files and POST them. This is the ONLY point at which file contents are read —
 * the preview the user confirmed was built from File.size alone, which costs no I/O. Same wire format
 * as the CLI (gzip-tar body + X-CCC-Meta header), so the server has exactly one upload path.
 */
export async function submit({
  picked, name, script, gpu, minutes, vram, node, args,
}: SubmitOpts): Promise<number> {
  const entries: TarEntry[] = []
  for (const f of picked.files) {
    entries.push({ path: relPath(f), data: new Uint8Array(await f.arrayBuffer()) })
  }
  const body = await gzip(makeTar(entries))
  // W&B is disabled for browser Spawns: the API key lives in the CLI's ~/.ccc/config, and the browser
  // has no equivalent yet. Send the same env the CLI sends when it has no key — an EMPTY wandb_env
  // would leave WANDB_MODE unset, which makes wandb.init() fail rather than skip.
  const meta = { name, script, gpu, minutes, vram, node, args, wandb_env: { WANDB_MODE: 'disabled' } }
  const r = await req('/submit', {
    method: 'POST',
    headers: { 'X-CCC-Meta': JSON.stringify(meta), 'Content-Type': 'application/gzip' },
    body,
  })
  return (await r.json()).id
}
