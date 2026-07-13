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

export type Arm = {
  name: string
  state: string
  gpus: number
  mesh_ip: string | null
  gpu_device: string | null
  admitted: boolean
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
}

export type Me = { username: string; is_admin: boolean; must_change_password: boolean }

export type SpawnStatus = {
  id: number
  done: boolean
  state: string
  exit?: number
}

export class Unauthorized extends Error {}

async function req(path: string, init?: RequestInit): Promise<Response> {
  const r = await fetch(path, init)
  if (r.status === 401) throw new Unauthorized('not logged in')
  if (!r.ok) {
    let detail = `${r.status}`
    try {
      detail = (await r.json()).detail ?? detail
    } catch {
      /* not JSON */
    }
    throw new Error(detail)
  }
  return r
}

const json = <T,>(path: string) => req(path).then((r) => r.json() as Promise<T>)

export const getArms = () => json<Arm[]>('/arms')
export const getSpawns = () => json<Spawn[]>('/spawns')
export const getMe = () => json<Me>('/me')
export const getStatus = (id: number) => json<SpawnStatus>(`/spawns/${id}`)
export const getLogs = (id: number) => req(`/spawns/${id}/logs`).then((r) => r.text())

export async function login(username: string, password: string): Promise<Me> {
  const r = await fetch('/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (r.status === 401) throw new Error('bad username or password')
  if (!r.ok) throw new Error(`login failed (${r.status})`)
  return r.json()
}

export async function logout(): Promise<void> {
  await fetch('/logout', { method: 'POST' })
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
  script: string
  gpu: number
  node: string | null
  args: string[]
}

/**
 * Tar + gzip the picked files and POST them. This is the ONLY point at which file contents are read —
 * the preview the user confirmed was built from File.size alone, which costs no I/O. Same wire format
 * as the CLI (gzip-tar body + X-CCC-Meta header), so the server has exactly one upload path.
 */
export async function submit({ picked, script, gpu, node, args }: SubmitOpts): Promise<number> {
  const entries: TarEntry[] = []
  for (const f of picked.files) {
    entries.push({ path: relPath(f), data: new Uint8Array(await f.arrayBuffer()) })
  }
  const body = await gzip(makeTar(entries))
  // W&B is disabled for browser Spawns: the API key lives in the CLI's ~/.ccc/config, and the browser
  // has no equivalent yet. Send the same env the CLI sends when it has no key — an EMPTY wandb_env
  // would leave WANDB_MODE unset, which makes wandb.init() fail rather than skip.
  const meta = { script, gpu, node, args, wandb_env: { WANDB_MODE: 'disabled' } }
  const r = await req('/submit', {
    method: 'POST',
    headers: { 'X-CCC-Meta': JSON.stringify(meta), 'Content-Type': 'application/gzip' },
    body,
  })
  return (await r.json()).id
}
