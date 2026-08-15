// The Head API, from the browser.
//
// Auth rides on the HttpOnly session cookie the Head sets at /login — the SPA never sees or stores the
// token (script can't read HttpOnly, so XSS can't steal it). Same origin as the API, so the cookie goes
// out automatically: no CORS, no Authorization header.
//
// Every route but /healthz is behind that session, so ANY call can come back 401, not just at login.
// `Unauthorized` is how the app notices and drops back to the login screen.

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
//
// `settling` is not an ending at all: it is the ≤30s in which a Spawn has left the queue and the karma
// tick has not yet stamped what it did (see outcomeOf). It exists so that gap has a word of its own —
// the alternative is reading an absent exit code as a failure, which is what it used to do.
export type Outcome = 'done' | 'failed' | 'cancelled' | 'retracted' | 'settling'

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
  // the Spawn wrote outside its declared outputs, held on the Head for a day. bytes rides alongside for
  // the same reason it does on RecoveryStatus: a size worth showing, not just a countdown.
  recovery_bytes?: number | null
  recovery_expires_at?: string | null
  // Output retention (docs/TODO.md "Storage retention"): when this Spawn's DECLARED results are deleted
  // — 7 days after it ended, or sooner if the submitter's 32GB Allowance needed the room. The more
  // consequential sibling of the pair above (these are the results the Spawn was submitted to produce).
  // Null once gone, or before the tick has registered them.
  output_bytes?: number | null
  output_expires_at?: string | null
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
  /** What RAN. Kept beside the Name rather than replaced by it — the page has room to say both, and
   *  "which script was this?" is a real question a queue row has no space to answer. */
  script?: string
  /** What a person CALLED it (CONTEXT.md, **Name**). What titles this page and every list row. */
  name?: string
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
  recovery_bytes?: number | null
  recovery_expires_at?: string | null
  output_bytes?: number | null
  output_expires_at?: string | null
}

// Recovery Window (docs/TODO.md): a one-day, 16GB-capped safety net for whatever a Spawn wrote OUTSIDE
// its declared $CCC_OUT — separate from (and never merged with) the permanent outputs tar. `expires_at`
// is an ISO timestamp; callers compute their own remaining-time countdown from it rather than a
// server-computed "seconds left" that would go stale the moment it's rendered.
export type RecoveryStatus = { bytes: number; expires_at: string }

// Output retention (docs/TODO.md "Storage retention"): the same two fields on the same terms, for the
// Spawn's DECLARED results — held 7 days from its end, and only while the submitter's 32GB Allowance has
// room. Identical shape to RecoveryStatus on purpose: the two panels render the same way, and a User
// learns one countdown rather than two.
export type RetentionStatus = { bytes: number; expires_at: string }

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

const json = <T,>(path: string) => req(path).then((r) => r.json() as Promise<T>)

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
  // The entry script (what RAN) and the **Name** (what a person called it). Two different facts: the
  // list is titled by the Name, and the script stays visible on the Spawn's own page.
  script: string
  name: string
  // What the submitter DEMANDED (Admin-only `--node`), which is null for almost every Spawn — never
  // where it ran. `placed_node` is that, recorded by the meter; null when the Spawn ended too fast to
  // be metered at all, or predates the column.
  node: string | null
  placed_node: string | null
  gpus: number
  minutes: number | null
  submitted_by: string | null
  cost: number | null
  recovery_bytes: number | null
  recovery_expires_at: string | null
  output_bytes: number | null
  output_expires_at: string | null
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
        // The **Name** its submitter gave it — not the script, which is what this used to show and is
        // why a Recipe run read `train.py` and a Workspace visit read nothing at all.
        name: r.name,
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
        recovery_bytes: r.recovery_bytes,
        recovery_expires_at: r.recovery_expires_at,
        output_bytes: r.output_bytes,
        output_expires_at: r.output_expires_at,
      }),
    ),
  )
export const getMe = () => json<Me>('/me')
export const getSpawn = (id: number) => json<SpawnDetail>(`/spawns/${id}`)
/** A Spawn's stderr — where a crash's traceback lands. `getLogs` serves stdout, which is empty on one. */
export const getStderr = (id: number) => req(`/spawns/${id}/stderr`).then((r) => r.text())
/** The Environment build's output — empty string for a Spawn that declared none. The Head's work, not
 *  the Spawn's, which is why it is its own stream rather than the head of stdout. */
// null = no open Recovery Window (never made, over the 16GB per-Spawn cap, evicted to make room for a
// newer copy of the submitter's own, or its day is up) — a normal outcome for a finished Spawn, not
// surfaced as an error to the caller.
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

// A Recipe: a frozen, re-runnable snapshot of a project — the code plus how to run it. Private to its
// owner. Runs exactly what it captured until Update code; `frozen_at` is how stale that snapshot is.
// `size_bytes` counts against the 32 GB allowance. `updating` is true only while a fresh snapshot
// uploads (the bloom) — at rest a frozen thing does not move.
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
  // The same two declared-need fields a Spawn carries (SubmitOpts below), frozen with the code rather
  // than asked again on every run. `features` is usually empty; `manifest` never is — a Recipe freezes
  // the environment its work runs in, not just the code, so naming one is required to save at all.
  manifest: string
  features: string[]
}

// Per-User storage allowance: 32 GB, a hard wall — saving past it means deleting a Recipe first, never a
// warning. Mirrored in recipes/format.ts (meter) and the Head's recipes.py MAX_USER_BYTES, the actual
// authority; the composer just blocks a doomed save early, from File.size, before spending the upload.
export const RECIPE_ALLOWANCE_BYTES = 32 * 1024 ** 3

export const getRecipes = () => json<Recipe[]>('/recipes')

// Freeze a project into a new Recipe. Same wire shape as submit() — tar+gzip body, meta in the header —
// because a Recipe IS the same upload a Spawn carries, just persisted on the Head instead of run once. The
// Head weighs it against the 32 GB wall and answers 413 if it won't fit (the composer blocks first, from
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
  // Same as above — asked once, frozen with the code. `manifest` is REQUIRED: a Recipe that named no
  // dependency file could never be run, so the Head refuses the save rather than freezing a dead one.
  manifest: string
  features: string[]
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
    // `features` is omitted rather than sent empty — absent means "no features required", which is
    // not the same instruction as any other value. `manifest` is required, so it always travels.
    ...(r.features.length ? { features: r.features } : {}),
    manifest: r.manifest.trim(),
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
 * Carries a body (unlike other actions here): an update IS a fresh upload, since there's no local copy of
 * the old folder to diff against and the Head keeps no Spawn code either. Until it lands, the Recipe keeps
 * running the old snapshot; a save the 32 GB wall refuses leaves that old snapshot untouched.
 */
export const updateRecipe = (name: string, r: NewRecipe) =>
  recipeUpload(r, `/recipes/${encodeURIComponent(name)}/update`)

// Everything about a Recipe except its code and name — what "Edit" changes when no folder is dropped
// alongside it (dropping one goes through updateRecipe above instead, a fresh upload). `script` still
// names a file, but one already sitting in the Recipe's saved tree rather than a freshly-picked folder's.
export type RecipeSettings = {
  script: string
  args: string[]
  outputs: string[]
  gpus: number
  vram_mb: number
  minutes: number
  wandb: boolean
  wandb_project: string
  wandb_entity: string
  manifest: string
  features: string[]
}

/**
 * Change a Recipe's settings in place — no code moves, so this is a plain JSON PATCH rather than the
 * tar+gzip body create/update share. `bytes` and `frozen_at` are untouched on the Head.
 */
export const updateRecipeSettings = (name: string, r: RecipeSettings) =>
  req(`/recipes/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(r),
  }).then((res) => res.json() as Promise<Recipe>)

/**
 * Run the frozen snapshot — one click, from any machine, nothing to hand. Every setting was already
 * answered when the Recipe was saved, so this takes no body. Returns the new Spawn's id; the Dashboard
 * doesn't need it (the card's own `running` flips true on the next refetch), but the CLI's `cuttle
 * recipe run` does, to echo it back the same way `cuttle run` echoes a fresh submit.
 */
export const runRecipe = (name: string) =>
  req(`/recipes/${encodeURIComponent(name)}/run`, { method: 'POST' })
    .then((r) => r.json() as Promise<{ id: number }>)
    .then((r) => r.id)
/** Delete a Recipe, freeing its bytes back to the allowance. */
export const deleteRecipe = (name: string) => req(`/recipes/${encodeURIComponent(name)}`, { method: 'DELETE' }).then(() => {})

// A Workspace: a working directory the Head KEEPS, so opening it again resumes where the last visit
// stopped. The Tide Pool's durable form, and the sibling of a Recipe — a Recipe is frozen and re-run in
// batch, a Workspace is mutable and visited interactively.
//
// `storage_bytes` is its **Storage**: what it may hold, carved out of the 32 GB workspace allowance by its
// owner when they created it. `used_bytes` is measured off the Head's disk on every read, never cached —
// a Workspace's tree changes every 60 seconds from inside a live session, over rsync, with nothing on
// the Head watching it happen.
//
// `live` is the Spawn id of the visit in progress, or null. `starting` covers the moment between the
// claim and sbatch answering, when a visit is live but has no id to show yet.
export type Workspace = {
  name: string
  storage_bytes: number     // its Storage — what it may hold
  used_bytes: number      // what it holds right now, off the disk
  gpus: number
  vram_mb: number
  minutes: number         // per-VISIT wall clock; the workspace itself never expires
  manifest: string        // optional here, unlike a Recipe's — a visit is a shell, not a run
  outputs: string[]       // optional too. $CCC_OUT is a folder INSIDE the tree
  features: string[]
  created_at: number | null
  last_visited_at: number | null
  live: number | null     // the Spawn id visiting it right now
  starting: boolean       // claimed, but sbatch has not answered yet
}

// Per-User: 32 GB, and the only allowance its holder divides up themselves — each Workspace reserves a
// Storage out of it at creation. Mirrors the Head's workspaces.py MAX_USER_BYTES, the actual authority.
export const WORKSPACE_ALLOWANCE_BYTES = 32 * 1024 ** 3

export const getWorkspaces = () => json<Workspace[]>('/workspaces')

// Seed a directory into a new Workspace — the ONLY upload one ever takes. Everything after this is
// written from inside the Workspace's own sessions and mirrored home, which is what makes it durable
// rather than a Recipe you re-freeze. Same wire shape as createRecipe: tar+gzip body, meta in the header.
export type NewWorkspace = {
  picked: Picked
  name: string
  storage_bytes: number
  gpus: number
  vram_mb: number
  minutes: number
  manifest: string
  outputs: string[]
  features: string[]
}

export async function createWorkspace(w: NewWorkspace): Promise<Workspace> {
  const entries: TarEntry[] = []
  for (const f of w.picked.files) {
    entries.push({ path: relPath(f), data: new Uint8Array(await f.arrayBuffer()) })
  }
  const body = await gzip(makeTar(entries))
  const meta = {
    name: w.name, storage_bytes: w.storage_bytes, gpus: w.gpus, vram_mb: w.vram_mb,
    minutes: w.minutes, manifest: w.manifest.trim(), outputs: w.outputs, features: w.features,
  }
  const res = await req('/workspaces', {
    method: 'POST',
    headers: { 'X-CCC-Meta': JSON.stringify(meta), 'Content-Type': 'application/gzip' },
    body,
  })
  return res.json() as Promise<Workspace>
}

// Everything a visit asks for, plus the Storage the tree may fill. There is no code half of this: a
// Workspace is seeded once and written to from inside its own sessions after that, so unlike a Recipe
// there is nothing to re-capture and no upload sibling of this call.
export type WorkspaceSettings = {
  storage_bytes: number
  gpus: number
  vram_mb: number
  minutes: number
  manifest: string
  outputs: string[]
  features: string[]
}

/**
 * Resize a Workspace, and change what its visits ask for. Never the files.
 *
 * The Head refuses a shrink below what the tree already holds (422, with the real number) and a grow
 * past the allowance (413) — a resize is weighed on what it GROWS BY, this Workspace's own reservation
 * coming off the pool first. A resize while a visit is live is accepted and takes effect on the NEXT
 * one: the running session's Storage is a constant baked into its job script.
 */
export const updateWorkspace = (name: string, w: WorkspaceSettings) =>
  req(`/workspaces/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(w),
  }).then((res) => res.json() as Promise<Workspace>)

/**
 * Open a Workspace: start a visit, or attach to the one already running.
 *
 * A visit is a Tide Pool bound to the tree — same queue, same wait for a card, same Karma — so the
 * connection details come back through the ordinary Tide Pool route once it is placed. A Workspace
 * holds ONE visit at a time, so this never queues a second behind a live one: `attached` says which of
 * the two happened.
 */
export const openWorkspace = (name: string) =>
  req(`/workspaces/${encodeURIComponent(name)}/open`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  }).then((r) => r.json() as Promise<{ id: number; attached: boolean }>)

/**
 * Whether the live visit's last mirror found the tree over its Storage, and by how much.
 *
 * The Head cannot see what the Arm is holding — only what the session's 60s mirror reports — so this is
 * a marker read, not a measurement. Worth surfacing because nothing fails loudly when a Workspace fills:
 * the session simply stops being saved.
 */
export const getWorkspaceFull = (name: string) =>
  json<{ live: number | null; full: boolean; bytes: number; storage_bytes: number }>(
    `/workspaces/${encodeURIComponent(name)}/full`)

/** Where the browser fetches a Workspace (or one folder inside it) as a tar. A plain link, not a fetch:
 *  a Storage runs to 16 GB and up, and the Head streams it rather than buffering — so the download belongs
 *  to the browser's own download manager, not to this tab's memory. */
export const workspaceDownloadUrl = (name: string, path?: string) =>
  `${API}/workspaces/${encodeURIComponent(name)}/download` +
  (path ? `?path=${encodeURIComponent(path)}` : '')

/** Delete a Workspace, freeing its Storage. Refused (409) while a visit is live. */
export const deleteWorkspace = (name: string) =>
  req(`/workspaces/${encodeURIComponent(name)}`, { method: 'DELETE' }).then(() => {})

// The W&B key the Head injects into every Spawn you submit (from either client). Held ENCRYPTED at rest —
// a third-party secret the Head keeps on your behalf, and unlike a password it must be recoverable in
// plaintext to be used, so it can't be hashed.
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
  /** What this run is called in the queue and the archive. REQUIRED — the Head refuses a nameless
   *  Spawn, and falls back to nothing (CONTEXT.md, **Name**). A label, not a key: two Spawns may
   *  share one, and the id is what tells them apart. */
  name: string
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
  // Which dependency file builds this Spawn's Python environment (docs/elements/environments.md),
  // relative to the project root. REQUIRED — nothing is detected and there is no default, because an
  // Environment carries its own Python and inherits nothing, so which file it comes from decides
  // everything the work runs against.
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
  // Head owns the key, client owns the run: send WANDB_PROJECT/WANDB_ENTITY only — never a key (browser
  // has none), never WANDB_MODE. Head fills in the key it holds for you.
  //
  // Blank fields are OMITTED, not sent empty: WANDB_ENTITY="" breaks wandb.init() instead of falling back
  // to the account default, and a blank project already maps to the Head's `ccc` default.
  //
  // `wandb` is the off-switch, travels OUTSIDE wandb_env: inside it, it'd have to be a WANDB_MODE, which
  // the Head no longer honours from a client. Head reads an absent flag as true, so it's always sent.
  const wandb_env: Record<string, string> = {}
  if (wandbProject.trim()) wandb_env.WANDB_PROJECT = wandbProject.trim()
  if (wandbEntity.trim()) wandb_env.WANDB_ENTITY = wandbEntity.trim()
  // Omitted rather than sent empty, matching wandb_env's fields above — most Spawns require none, and
  // the Head reads an absent key the same as an empty list.
  const meta = {
    name, script, gpu, minutes, vram, node, args, wandb, wandb_env, outputs,
    ...(features.length ? { features } : {}),
    // Always sent, unlike the fields above: the Head refuses a Spawn that names no dependency file, and
    // an omitted key would meet that refusal as a confusing "missing" rather than the empty box it is.
    manifest: manifest.trim(),
  }
  const r = await req('/submit', {
    method: 'POST',
    headers: { 'X-CCC-Meta': JSON.stringify(meta), 'Content-Type': 'application/gzip' },
    body,
  })
  const body_ = await r.json()
  return { id: body_.id, environment: body_.environment ?? null }
}

// A Tide Pool (docs/TODO.md): request one from the Dashboard, same queue/Eligibility/Escrow as any Spawn
// — but no script to name, so deliberately NOT `submit` with fields blanked out. Project folder is
// optional (unlike Script mode): picking one just lands it at $proj on the Arm for the live shell, nothing
// runs automatically. Shell access is CLI-only; Dashboard's part ends once the Spawn appears in the queue.
export type SubmitTidepoolOpts = {
  minutes: number
  vram: number
  gpu: number
  node: string | null
  features: string[]
  picked: Picked | null
  /** Required, exactly as it is for an ordinary Spawn: a Tide Pool may decline to declare an
   *  Environment, and may not decline to be named. */
  name: string
  /** OPTIONAL here and only here — a Tide Pool may decline to declare an Environment. Blank gives a
   *  bare shell; a name builds that env into it, and needs `picked` to resolve against. */
  manifest?: string
}

export async function submitTidepool({
  minutes, vram, gpu, node, features, picked, name, manifest,
}: SubmitTidepoolOpts): Promise<number> {
  // A genuinely empty gzip-tar when no project was picked, not an empty body — the Head always opens the
  // upload as a tar archive (ccc_head_api.spawns.submit). `makeTar([])` writes the two all-zero
  // end-of-archive blocks tar readers expect, same as Python's tarfile for no members.
  const entries: TarEntry[] = []
  for (const f of picked?.files ?? []) {
    entries.push({ path: relPath(f), data: new Uint8Array(await f.arrayBuffer()) })
  }
  const body = await gzip(makeTar(entries))
  const meta = {
    minutes, vram, gpu, node, tidepool: true, outputs: [], name,
    ...(features.length ? { features } : {}),
    // Omitted rather than sent empty, unlike submit()'s above: a Tide Pool is the one Spawn the Head
    // lets decline to declare an Environment, so absent here means "a bare shell" rather than an
    // error. Naming one requires the picked folder — it is built from the upload.
    ...(manifest?.trim() ? { manifest: manifest.trim() } : {}),
  }
  const r = await req('/submit', {
    method: 'POST',
    headers: { 'X-CCC-Meta': JSON.stringify(meta), 'Content-Type': 'application/gzip' },
    body,
  })
  return (await r.json()).id
}
