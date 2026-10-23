import { useCallback, useEffect, useState } from "react";
import {
  addSshKey, errMessage, getSshKeys, getTidepool, removeSshKey, tidepoolAct, Unauthorized,
  type SshKey, type Tidepool,
} from "../api";
import { Tip } from "../app/Tip";

/**
 * Your Tidepool — the always-on CPU shell + files on the Head. State, disk use against its fixed size,
 * how to get in, restart/reset, and the SSH keys allowed in. `cuttle login` adds each box's own key;
 * this is where another (a second laptop, VS Code on a machine without cuttle) gets added by hand.
 *
 * Reset asks twice inline (no browser dialog): it drops everything installed outside your home.
 */
export function TidepoolCard({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [tp, setTp] = useState<Tidepool | null>(null);
  const [keys, setKeys] = useState<SshKey[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [armed, setArmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [pub, setPub] = useState("");

  const fail = useCallback((e: unknown) => {
    if (e instanceof Unauthorized) onUnauthorized();
    else setError(errMessage(e));
  }, [onUnauthorized]);

  const load = useCallback(() => {
    getTidepool().then(setTp).catch(fail);
    getSshKeys().then(setKeys).catch(fail);
  }, [fail]);

  useEffect(load, [load]);

  const act = async (verb: "restart" | "reset") => {
    setBusy(verb); setError(null); setArmed(false);
    try { await tidepoolAct(verb); load(); } catch (e) { fail(e); } finally { setBusy(null); }
  };

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try { await addSshKey(pub.trim()); setPub(""); setAdding(false); load(); } catch (err) { fail(err); }
  };

  const remove = async (id: number) => {
    setError(null);
    try { await removeSshKey(id); load(); } catch (e) { fail(e); }
  };

  const up = tp?.state === "active";
  const usage = tp && tp.size_gb ? `${tp.used_gb.toFixed(1)} / ${tp.size_gb} GB` : "—";
  const pct = tp && tp.size_gb ? Math.min(100, (tp.used_gb / tp.size_gb) * 100) : 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <Tip className="label" text="Your files and an always-on CPU shell on the Head. GPUs come only with a queued Dive. A full Tidepool refuses writes; nothing else is affected.">
          Tidepool
        </Tip>
        <span className="text-[13px]" style={{ color: up ? "var(--irid-mid)" : "var(--alarm)" }}>
          {tp ? tp.state : "—"}
        </span>
      </div>

      <div>
        <div className="flex justify-between text-[13px] text-ink-soft"><span>disk</span><span className="num">{usage}</span></div>
        <div className="mt-1 h-1.5 rounded-full" style={{ background: "rgba(255,255,255,0.08)" }}>
          <div className="h-1.5 rounded-full" style={{ width: `${pct}%`, background: pct > 90 ? "var(--alarm)" : "var(--irid-mid)" }} />
        </div>
      </div>

      <div className="text-[13px] text-ink-soft">
        <code className="num">ssh {tp?.alias ?? "glados"}</code>
        <span> · or </span><code className="num">cuttle tidepool</code>
        <span> · VS Code Remote-SSH host </span><code className="num">{tp?.alias ?? "glados"}</code>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-ghost" disabled={busy !== null} onClick={() => act("restart")}>
          {busy === "restart" ? "Restarting…" : "Restart"}
        </button>
        {!armed ? (
          <button className="btn-ghost" disabled={busy !== null} onClick={() => setArmed(true)}>
            {busy === "reset" ? "Resetting…" : "Reset"}
          </button>
        ) : (
          <>
            <span className="text-[13px] text-[var(--alarm)]">Drops everything installed outside your home.</span>
            <button className="btn-primary" onClick={() => act("reset")}>Reset it</button>
            <button className="btn-ghost" onClick={() => setArmed(false)}>Cancel</button>
          </>
        )}
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-3">
          <span className="label">SSH keys</span>
          {!adding && <button className="btn-ghost" onClick={() => setAdding(true)}>Add a key</button>}
        </div>
        {keys.length === 0 && <p className="text-[13px] text-ink-soft">None yet — <code>cuttle login</code> adds this machine's.</p>}
        {keys.map((k) => (
          <div key={k.id} className="flex items-center justify-between gap-3 text-[13px]">
            <span className="truncate text-ink-soft" title={k.pubkey}>
              {k.name ?? "key"} <span className="num">{k.pubkey.split(" ")[1]?.slice(-16)}</span>
            </span>
            <button className="btn-ghost" onClick={() => remove(k.id)}>Remove</button>
          </div>
        ))}
        {adding && (
          <form onSubmit={add} className="space-y-2">
            <textarea className="field" rows={3} placeholder="ssh-ed25519 AAAA… (the contents of a .pub file)"
              value={pub} onChange={(e) => setPub(e.target.value)} />
            <div className="flex gap-2">
              <button className="btn-primary" disabled={!pub.trim()}>Add</button>
              <button type="button" className="btn-ghost" onClick={() => { setPub(""); setAdding(false); }}>Cancel</button>
            </div>
          </form>
        )}
      </div>

      {error && <p className="text-[13px] text-[var(--alarm)]">{error}</p>}
    </div>
  );
}
