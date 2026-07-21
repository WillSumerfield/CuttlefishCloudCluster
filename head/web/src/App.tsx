import { useCallback, useEffect, useRef, useState } from 'react'
import {
  changePassword,
  downloadOutputs,
  getArms,
  getLogs,
  getMe,
  getSpawns,
  getStatus,
  login,
  logout,
  submit,
  Unauthorized,
  type Arm,
  type Me,
  type Spawn,
  type SpawnStatus,
} from './api'
import { human, pick, type Picked } from './project'

const POLL_MS = 3000

// --- shared chrome ----------------------------------------------------------

function pillClass(state: string): string {
  const s = state.toLowerCase()
  if (/(down|drain|fail|err|unknown)/.test(s)) return 'bg-rose-500/15 text-rose-300 ring-rose-500/30'
  if (/(alloc|mix|running|comp)/.test(s)) return 'bg-sky-500/15 text-sky-300 ring-sky-500/30'
  if (/(pending|configuring)/.test(s)) return 'bg-amber-500/15 text-amber-300 ring-amber-500/30'
  if (/idle|done/.test(s)) return 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30'
  return 'bg-slate-500/15 text-slate-300 ring-slate-500/30'
}

function Pill({ state }: { state: string }) {
  return (
    <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${pillClass(state)}`}>
      {state || '—'}
    </span>
  )
}

function Card({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/50 shadow-sm">
      <header className="flex items-center gap-2 border-b border-slate-800 px-4 py-3">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200 uppercase">{title}</h2>
        {count !== undefined && (
          <span className="rounded-full bg-slate-800 px-2 py-0.5 text-xs text-slate-400">{count}</span>
        )}
      </header>
      <div className="overflow-x-auto">{children}</div>
    </section>
  )
}

const th = 'px-4 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500'
const td = 'px-4 py-2 text-sm text-slate-300 whitespace-nowrap'
const input =
  'w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 ' +
  'placeholder:text-slate-600 focus:border-sky-500 focus:outline-none'
const btn =
  'rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 ' +
  'disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-500'

// --- login ------------------------------------------------------------------

// Hands the password back up alongside the user: a must-change account goes straight from here to
// ChangePassword, which needs it as `old_password` and must never ask for it a second time.
function Login({ onLogin }: { onLogin: (me: Me, password: string) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submitLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onLogin(await login(username, password), password)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 px-4">
      <form onSubmit={submitLogin} className="w-full max-w-sm space-y-4">
        <h1 className="text-center text-2xl font-bold tracking-tight text-slate-100">
          <span className="text-sky-400">CCC</span> Dashboard
        </h1>
        <input className={input} placeholder="Username" autoFocus autoComplete="username"
               value={username} onChange={(e) => setUsername(e.target.value)} />
        <input className={input} type="password" placeholder="Password"
               autoComplete="current-password" value={password}
               onChange={(e) => setPassword(e.target.value)} />
        {error && <p className="text-sm text-rose-400">{error}</p>}
        <button className={`${btn} w-full`} disabled={busy || !username || !password}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}

/** Forced rotation: a freshly-provisioned account holds a temp password it must replace before it can
 * submit anything (the API 403s /submit until it does).
 *
 * `current` is the password the user just signed in with, carried over in memory. Asking for it again
 * here would be both redundant (they typed it one screen ago) and a trap: two password boxes read as
 * "new + confirm" to everyone, so the temp password lands in the wrong one and /passwd 403s. The two
 * boxes below therefore ARE new + confirm — the shape people expect, and the one `cuttle passwd` uses.
 */
function ChangePassword({ current, onDone }: { current: string; onDone: () => void }) {
  const [newPw, setNewPw] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mismatch = confirm.length > 0 && newPw !== confirm

  const go = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (newPw !== confirm) return setError('the two passwords do not match')
    try {
      await changePassword(current, newPw)
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 px-4">
      <form onSubmit={go} className="w-full max-w-sm space-y-4">
        <h1 className="text-lg font-semibold text-slate-100">Choose a new password</h1>
        <p className="text-sm text-slate-400">
          Your account was created with a temporary password. Replace it before submitting work.
        </p>
        <label className="block space-y-1">
          <span className="text-xs text-slate-400">New password</span>
          <input className={input} type="password" autoFocus autoComplete="new-password"
                 value={newPw} onChange={(e) => setNewPw(e.target.value)} />
        </label>
        <label className="block space-y-1">
          <span className="text-xs text-slate-400">Confirm new password</span>
          <input className={input} type="password" autoComplete="new-password" value={confirm}
                 onChange={(e) => setConfirm(e.target.value)} />
        </label>
        {(error || mismatch) && (
          <p className="text-sm text-rose-400">{error ?? 'the two passwords do not match'}</p>
        )}
        <button className={`${btn} w-full`} disabled={!newPw || newPw !== confirm}>
          Change password
        </button>
      </form>
    </div>
  )
}

// --- submit -----------------------------------------------------------------

/**
 * Folder -> Spawn. The two-step shape is deliberate: picking a folder reads NOTHING off disk and
 * uploads NOTHING. It only counts files and sums File.size (free), and shows you the result. The tar,
 * the gzip and the POST happen when — and only when — you press Submit. That preview is the safety net
 * for the fact that our .gitignore matching can't be exactly git's (see project.ts): if the filter is
 * wrong, you see "1,204 files, 3.9 GB" and stop, instead of discovering it afterwards.
 */
function SubmitPanel({
  arms, isAdmin, onSubmitted,
}: { arms: Arm[]; isAdmin: boolean; onSubmitted: (id: number) => void }) {
  const [picked, setPicked] = useState<Picked | null>(null)
  const [honor, setHonor] = useState(true)
  const [script, setScript] = useState('')
  const [gpu, setGpu] = useState(1)
  // No defaults, and no autofill from the fleet's biggest card: an untouched --vram that meant "the
  // largest GPU we have" would make every casual Spawn eligible for that one card alone — queued
  // behind everything, at the highest Demand, for work that may need 2 GB.
  const [minutes, setMinutes] = useState('')
  const [vram, setVram] = useState('')
  const [node, setNode] = useState('')
  const [args, setArgs] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const lastList = useRef<FileList | null>(null)

  const rescan = useCallback(async (list: FileList | null, honorGitignore: boolean) => {
    if (!list || list.length === 0) return
    setPicked(await pick(list, honorGitignore))
  }, [])

  const onPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    lastList.current = e.target.files
    setError(null)
    await rescan(e.target.files, honor)
  }

  const toggleHonor = async (v: boolean) => {
    setHonor(v)
    await rescan(lastList.current, v) // re-filter the same selection; still no upload
  }

  const ready = picked && script && Number(minutes) > 0 && Number(vram) > 0

  const go = async () => {
    if (!ready) return
    setBusy(true)
    setError(null)
    try {
      const id = await submit({
        picked,
        name: null,
        script,
        gpu,
        minutes: Number(minutes),
        vram: Number(vram),
        node: node || null,
        args: args.split(/\s+/).filter(Boolean),
      })
      onSubmitted(id)
      setPicked(null)
      if (fileRef.current) fileRef.current.value = ''
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="New Spawn">
      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-3">
          {/* webkitdirectory: React doesn't type it, so it's set via ref-free attribute spread. */}
          <input
            ref={fileRef}
            type="file"
            onChange={onPick}
            {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
            className="text-sm text-slate-400 file:mr-3 file:rounded-md file:border-0 file:bg-slate-800
                       file:px-3 file:py-2 file:text-sm file:text-slate-200 hover:file:bg-slate-700"
          />
          <label className="flex items-center gap-2 text-sm text-slate-400">
            <input type="checkbox" checked={honor} onChange={(e) => toggleHonor(e.target.checked)} />
            honor .gitignore
          </label>
        </div>

        {picked && (
          <div className="rounded-md border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm">
            <span className="font-medium text-slate-200">{picked.root}</span>
            <span className="text-slate-500">
              {' '}
              — {picked.files.length} of {picked.scanned} files,{' '}
              <span className="text-slate-300">{human(picked.bytes)}</span> will upload
            </span>
            {picked.bytes > 1024 ** 3 && (
              <p className="mt-1 text-xs text-amber-400">
                That's over a gigabyte. Check your .gitignore excludes data and checkpoints.
              </p>
            )}
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-4">
          <input className={`${input} sm:col-span-2`} placeholder="script (e.g. train.py)"
                 value={script} onChange={(e) => setScript(e.target.value)} />
          <input className={input} type="number" min={1} placeholder="minutes"
                 value={minutes} onChange={(e) => setMinutes(e.target.value)} />
          <input className={input} type="number" min={1} placeholder="VRAM (GB)"
                 value={vram} onChange={(e) => setVram(e.target.value)} />
          <input className={input} type="number" min={0} value={gpu}
                 onChange={(e) => setGpu(Number(e.target.value))} />
          {/* Naming a machine is admin-only: a User says what they NEED, and Slurm gives them the
              fastest free Arm that has it. Non-admins never see this control (and the Head 403s it). */}
          {isAdmin && (
            <select className={input} value={node} onChange={(e) => setNode(e.target.value)}>
              <option value="">any Arm</option>
              {arms.map((a) => (
                <option key={a.name} value={a.name}>{a.name}</option>
              ))}
            </select>
          )}
          <input className={`${input} ${isAdmin ? 'sm:col-span-2' : 'sm:col-span-3'}`}
                 placeholder="extra args (optional)"
                 value={args} onChange={(e) => setArgs(e.target.value)} />
        </div>

        <p className="text-xs text-slate-500">
          The Spawn is killed at its time limit. Every Arm with at least this much VRAM is eligible —
          over-estimating either one costs you queue position, not Karma.
        </p>

        {error && <p className="text-sm text-rose-400">{error}</p>}

        <button className={btn} disabled={!ready || busy} onClick={go}>
          {busy ? `Uploading ${human(picked?.bytes ?? 0)}…` : 'Submit Spawn'}
        </button>
      </div>
    </Card>
  )
}

// --- board ------------------------------------------------------------------

function ArmsTable({ arms }: { arms: Arm[] }) {
  return (
    <table className="min-w-full divide-y divide-slate-800">
      <thead>
        <tr>
          <th className={th}>Name</th>
          <th className={th}>Owner</th>
          <th className={th}>State</th>
          <th className={th}>GPU</th>
          <th className={th}>VRAM</th>
          {/* Power: measured TFLOP/s. Half of what a second on this card costs (the other half, Demand,
              is live). '—' until the Arm has been benchmarked. */}
          <th className={th}>Power</th>
          <th className={th}>Mesh IP</th>
          <th className={th}>Admitted</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-800/60">
        {arms.map((a) => (
          <tr key={a.name} className="hover:bg-slate-800/30">
            <td className={`${td} font-medium text-slate-100`}>{a.name}</td>
            <td className={td}>{a.owner ?? '—'}</td>
            <td className={td}><Pill state={a.state} /></td>
            <td className={td}>{a.gpu ?? '—'}</td>
            <td className={td}>{a.vram_mb ? `${Math.round(a.vram_mb / 1024)} GB` : '—'}</td>
            <td className={td}>{a.power ? `${a.power} TF` : '—'}</td>
            <td className={`${td} font-mono text-slate-400`}>{a.mesh_ip ?? '—'}</td>
            <td className={td}>{a.admitted ? '✓' : '—'}</td>
          </tr>
        ))}
        {arms.length === 0 && <EmptyRow cols={8} label="no arms" />}
      </tbody>
    </table>
  )
}

function SpawnsTable({ spawns, onOpen }: { spawns: Spawn[]; onOpen: (id: number) => void }) {
  return (
    <table className="min-w-full divide-y divide-slate-800">
      <thead>
        <tr>
          <th className={th}>ID</th>
          <th className={th}>Name</th>
          <th className={th}>State</th>
          <th className={th}>Node</th>
          <th className={th}>GPUs</th>
          {/* Attribution, NOT Slurm's owner field — every Spawn runs as the Mantle, so Slurm would
              say `ccc` for all of them. This is the only place a Spawn is tied to a person. */}
          <th className={th}>Submitted by</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-800/60">
        {spawns.map((s) => (
          <tr key={s.id} className="cursor-pointer hover:bg-slate-800/30" onClick={() => onOpen(s.id)}>
            <td className={`${td} font-mono text-sky-400`}>{s.id}</td>
            <td className={`${td} font-medium text-slate-100`}>{s.name}</td>
            <td className={td}><Pill state={s.state} /></td>
            <td className={td}>{s.node ?? '—'}</td>
            <td className={td}>{s.gpus}</td>
            <td className={td}>{s.submitted_by ?? '—'}</td>
          </tr>
        ))}
        {spawns.length === 0 && <EmptyRow cols={6} label="queue empty" />}
      </tbody>
    </table>
  )
}

function EmptyRow({ cols, label }: { cols: number; label: string }) {
  return (
    <tr>
      <td className="px-4 py-8 text-center text-sm text-slate-600" colSpan={cols}>{label}</td>
    </tr>
  )
}

// --- spawn detail -----------------------------------------------------------

/** One Spawn: its state and its stdout. A co-located Spawn's log grows live (it writes straight to the
 * Head's disk); a remote Arm's arrives in one lump at stage-out, so the pane simply stays empty until
 * it lands — that's expected, not a hang. */
function SpawnDetail({ id, onClose }: { id: number; onClose: () => void }) {
  const [status, setStatus] = useState<SpawnStatus | null>(null)
  const [logs, setLogs] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setInterval> | undefined
    const tick = async () => {
      try {
        const [st, text] = await Promise.all([getStatus(id), getLogs(id)])
        if (!alive) return
        setStatus(st)
        setLogs(text)
        if (st.done && timer) clearInterval(timer) // finished: stop polling a Spawn that can't change
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      }
    }
    timer = setInterval(tick, POLL_MS)
    tick()
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [id])

  const verdict = !status
    ? '…'
    : !status.done
      ? status.state
      : status.state === 'failed'
        ? 'FAILED — killed, or its Arm dropped before staging results home'
        : `done (exit ${status.exit})`

  return (
    <Card title={`Spawn ${id}`}>
      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Pill state={status?.done ? status.state : (status?.state ?? '')} />
          <span className="text-sm text-slate-400">{verdict}</span>
          <div className="ml-auto flex gap-2">
            <button className={btn} disabled={!status?.done} onClick={() => downloadOutputs(id)}>
              Download outputs
            </button>
            <button className="rounded-md border border-slate-700 px-4 py-2 text-sm text-slate-300
                               hover:bg-slate-800" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
        {error && <p className="text-sm text-rose-400">{error}</p>}
        <pre className="max-h-96 overflow-auto rounded-md border border-slate-800 bg-slate-950 p-3
                        font-mono text-xs text-slate-300">
          {logs || <span className="text-slate-600">no output yet</span>}
        </pre>
      </div>
    </Card>
  )
}

// --- app --------------------------------------------------------------------

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  // The password from this session's sign-in, kept in memory ONLY to feed the forced-rotation screen
  // (see ChangePassword). Never stored, never leaves the tab, dropped the moment rotation succeeds.
  const [signInPw, setSignInPw] = useState<string | null>(null)
  const [booting, setBooting] = useState(true)
  const [arms, setArms] = useState<Arm[]>([])
  const [spawns, setSpawns] = useState<Spawn[]>([])
  const [open, setOpen] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [updated, setUpdated] = useState<Date | null>(null)

  // A cookie session may already be live from a previous visit, so ask who we are before showing a
  // login form the user doesn't need.
  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setBooting(false))
  }, [])

  useEffect(() => {
    if (!me || me.must_change_password) return
    let alive = true
    const tick = async () => {
      try {
        const [a, s] = await Promise.all([getArms(), getSpawns()])
        if (!alive) return
        setArms(a)
        setSpawns(s)
        setUpdated(new Date())
        setError(null)
      } catch (e) {
        if (!alive) return
        // The session can expire while the board is open — fall back to login rather than showing a
        // board that is quietly no longer updating.
        if (e instanceof Unauthorized) setMe(null)
        else setError(e instanceof Error ? e.message : String(e))
      }
    }
    tick()
    const timer = setInterval(tick, POLL_MS)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [me])

  if (booting) return <div className="min-h-screen bg-slate-950" />
  // A live cookie with no password in memory (a reload mid-rotation, or a revisit) can't answer
  // /passwd's old_password — so send them back through sign-in to re-supply it, rather than growing a
  // second form shape that asks for the temp password on one path only.
  const pw = signInPw
  if (!me || (me.must_change_password && pw === null)) {
    return <Login onLogin={(u, typed) => { setMe(u); setSignInPw(typed) }} />
  }
  if (me.must_change_password && pw !== null) {
    return (
      <ChangePassword
        current={pw}
        onDone={() => { setSignInPw(null); setMe({ ...me, must_change_password: false }) }}
      />
    )
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-5xl px-4 py-8">
        <header className="mb-6 flex items-baseline justify-between">
          <h1 className="text-2xl font-bold tracking-tight">
            <span className="text-sky-400">CCC</span> Dashboard
          </h1>
          <div className="text-right text-xs text-slate-500">
            {error ? (
              <span className="text-rose-400">● {error}</span>
            ) : (
              <span>● live · updated {updated ? updated.toLocaleTimeString() : '…'}</span>
            )}
            <span className="ml-3 text-slate-400">
              {me.username}
              {me.is_admin && <span className="text-sky-400"> (admin)</span>}
            </span>
            <button
              className="ml-2 text-slate-500 underline hover:text-slate-300"
              onClick={async () => {
                await logout()
                setMe(null)
              }}
            >
              sign out
            </button>
          </div>
        </header>

        <div className="grid gap-6">
          <SubmitPanel arms={arms} isAdmin={me.is_admin} onSubmitted={setOpen} />
          {open !== null && <SpawnDetail id={open} onClose={() => setOpen(null)} />}
          <Card title="Arms" count={arms.length}>
            <ArmsTable arms={arms} />
          </Card>
          <Card title="Spawns" count={spawns.length}>
            <SpawnsTable spawns={spawns} onOpen={setOpen} />
          </Card>
        </div>
      </div>
    </div>
  )
}
