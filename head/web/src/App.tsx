import { useEffect, useState } from 'react'

// Mirrors the dashboard app's projection (ccc_head_api/slurm.py). Read-only for now; job control and
// submission arrive with the Phase-3 login.
type Arm = {
  name: string
  state: string
  gpus: number
  mesh_ip: string | null
  gpu_device: string | null
  admitted: boolean
}
type Spawn = {
  id: number
  name: string
  state: string
  node: string | null
  gpus: number
  user: string
  submit_time: number | null
  start_time: number | null
}

const POLL_MS = 3000

// One poller for both endpoints. Keeps the last good data on a transient error so the board doesn't
// flash empty between ticks; surfaces the error instead.
function usePoll(): { arms: Arm[]; spawns: Spawn[]; error: string | null; updated: Date | null } {
  const [arms, setArms] = useState<Arm[]>([])
  const [spawns, setSpawns] = useState<Spawn[]>([])
  const [error, setError] = useState<string | null>(null)
  const [updated, setUpdated] = useState<Date | null>(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const [a, s] = await Promise.all([
          fetch('/arms').then((r) => (r.ok ? r.json() : Promise.reject(new Error(`arms ${r.status}`)))),
          fetch('/spawns').then((r) => (r.ok ? r.json() : Promise.reject(new Error(`spawns ${r.status}`)))),
        ])
        if (!alive) return
        setArms(a)
        setSpawns(s)
        setUpdated(new Date())
        setError(null)
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      }
    }
    tick()
    const id = setInterval(tick, POLL_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  return { arms, spawns, error, updated }
}

// Slurm state string (lowercased, possibly compound like "idle+drain") -> pill colour.
function pill(state: string): string {
  const s = state.toLowerCase()
  if (/(down|drain|fail|err|unknown)/.test(s)) return 'bg-rose-500/15 text-rose-300 ring-rose-500/30'
  if (/(alloc|mix|running|comp)/.test(s)) return 'bg-sky-500/15 text-sky-300 ring-sky-500/30'
  if (/(pending|configuring)/.test(s)) return 'bg-amber-500/15 text-amber-300 ring-amber-500/30'
  if (/idle/.test(s)) return 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30'
  return 'bg-slate-500/15 text-slate-300 ring-slate-500/30'
}

function Pill({ state }: { state: string }) {
  return (
    <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${pill(state)}`}>
      {state || '—'}
    </span>
  )
}

function Card({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/50 shadow-sm">
      <header className="flex items-center gap-2 border-b border-slate-800 px-4 py-3">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200 uppercase">{title}</h2>
        <span className="rounded-full bg-slate-800 px-2 py-0.5 text-xs text-slate-400">{count}</span>
      </header>
      <div className="overflow-x-auto">{children}</div>
    </section>
  )
}

const th = 'px-4 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500'
const td = 'px-4 py-2 text-sm text-slate-300 whitespace-nowrap'

function ArmsTable({ arms }: { arms: Arm[] }) {
  return (
    <table className="min-w-full divide-y divide-slate-800">
      <thead>
        <tr>
          <th className={th}>Name</th>
          <th className={th}>State</th>
          <th className={th}>GPUs</th>
          <th className={th}>Mesh IP</th>
          <th className={th}>Admitted</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-800/60">
        {arms.map((a) => (
          <tr key={a.name} className="hover:bg-slate-800/30">
            <td className={`${td} font-medium text-slate-100`}>{a.name}</td>
            <td className={td}><Pill state={a.state} /></td>
            <td className={td}>{a.gpus}</td>
            <td className={`${td} font-mono text-slate-400`}>{a.mesh_ip ?? '—'}</td>
            <td className={td}>{a.admitted ? '✓' : '—'}</td>
          </tr>
        ))}
        {arms.length === 0 && <EmptyRow cols={5} label="no arms" />}
      </tbody>
    </table>
  )
}

function SpawnsTable({ spawns }: { spawns: Spawn[] }) {
  return (
    <table className="min-w-full divide-y divide-slate-800">
      <thead>
        <tr>
          <th className={th}>ID</th>
          <th className={th}>Name</th>
          <th className={th}>State</th>
          <th className={th}>Node</th>
          <th className={th}>GPUs</th>
          <th className={th}>User</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-800/60">
        {spawns.map((s) => (
          <tr key={s.id} className="hover:bg-slate-800/30">
            <td className={`${td} font-mono text-slate-400`}>{s.id}</td>
            <td className={`${td} font-medium text-slate-100`}>{s.name}</td>
            <td className={td}><Pill state={s.state} /></td>
            <td className={td}>{s.node ?? '—'}</td>
            <td className={td}>{s.gpus}</td>
            <td className={td}>{s.user}</td>
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
      <td className="px-4 py-8 text-center text-sm text-slate-600" colSpan={cols}>
        {label}
      </td>
    </tr>
  )
}

export default function App() {
  const { arms, spawns, error, updated } = usePoll()
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
          </div>
        </header>
        <div className="grid gap-6">
          <Card title="Arms" count={arms.length}>
            <ArmsTable arms={arms} />
          </Card>
          <Card title="Spawns" count={spawns.length}>
            <SpawnsTable spawns={spawns} />
          </Card>
        </div>
      </div>
    </div>
  )
}
