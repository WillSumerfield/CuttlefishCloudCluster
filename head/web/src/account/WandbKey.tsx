import { useCallback, useEffect, useState } from "react";
import {
  getWandbKey,
  revealWandbKey,
  setWandbKey,
  deleteWandbKey,
  Unauthorized,
  type WandbKeyStatus,
} from "../api";

/**
 * The W&B key module — a stored secret, masked by default. Reveal fetches the plaintext (so you can check
 * you pasted the right one); Replace overwrites it; Remove clears it. Unset, it's an invitation to paste
 * one, with a line on what it does.
 *
 * The Head serves no /account/wandb route yet, so live this loads to "not available yet"; under ?demo it
 * runs on a local copy seeded from the mock persona. `demoKey` is that seed (undefined once cleared).
 */

type State =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "status"; status: WandbKeyStatus };

export function WandbKey({
  demo,
  demoKey,
  onUnauthorized,
}: {
  demo: boolean;
  demoKey?: string;
  onUnauthorized: () => void;
}) {
  // The demo secret lives here, mutable across replace/remove. Live, the real secret never enters the SPA
  // except for the one deliberate Reveal.
  const [localKey, setLocalKey] = useState<string | undefined>(demoKey);
  const [state, setState] = useState<State>({ kind: "loading" });
  const [revealed, setRevealed] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const maskFrom = (key?: string): State =>
    key ? { kind: "status", status: { set: true, last4: key.slice(-4) } } : { kind: "status", status: { set: false, last4: null } };

  const load = useCallback(() => {
    if (demo) { setState(maskFrom(localKey)); return; }
    getWandbKey()
      .then((status) => setState({ kind: "status", status }))
      .catch((e) => (e instanceof Unauthorized ? onUnauthorized() : setState({ kind: "unavailable" })));
  }, [demo, localKey, onUnauthorized]);

  useEffect(() => { load(); }, [load]);

  const reveal = async () => {
    if (demo) { setRevealed(localKey ?? ""); return; }
    try {
      setRevealed(await revealWandbKey());
    } catch (e) {
      if (e instanceof Unauthorized) onUnauthorized();
    }
  };

  const save = async () => {
    const key = draft.trim();
    if (!key) return;
    setBusy(true);
    if (demo) {
      setLocalKey(key);
      setState(maskFrom(key));
      setEditing(false);
      setDraft("");
      setRevealed(null);
      setBusy(false);
      return;
    }
    try {
      await setWandbKey(key);
      setEditing(false);
      setDraft("");
      setRevealed(null);
      load();
    } catch (e) {
      if (e instanceof Unauthorized) onUnauthorized();
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setRevealed(null);
    if (demo) { setLocalKey(undefined); setState(maskFrom(undefined)); return; }
    try {
      await deleteWandbKey();
      load();
    } catch (e) {
      if (e instanceof Unauthorized) onUnauthorized();
    }
  };

  return (
    <div>
      <div className="label mb-2">W&amp;B key</div>

      {state.kind === "loading" && <p className="text-[13px] text-ink-mute">Loading…</p>}

      {state.kind === "unavailable" && (
        <p className="text-[13px] text-ink-mute">Not available yet — the Head doesn't serve this route.</p>
      )}

      {state.kind === "status" && (
        <>
          {editing || !state.status.set ? (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <input
                  className="field min-w-0 flex-1"
                  autoFocus={editing}
                  placeholder="paste your W&B API key"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && save()}
                />
                <button className="btn-primary shrink-0" disabled={busy || !draft.trim()} onClick={save}>
                  {busy ? "Saving…" : "Save"}
                </button>
                {editing && (
                  <button className="btn-ghost shrink-0" onClick={() => { setEditing(false); setDraft(""); }}>
                    Cancel
                  </button>
                )}
              </div>
              {!state.status.set && (
                <p className="text-[12px] text-ink-mute">
                  Injected into every spawn you submit, from either client. Held encrypted.
                </p>
              )}
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <span className="num text-[13px] text-ink-soft">
                {revealed !== null ? revealed : `•••••••••• ${state.status.last4 ?? ""}`}
              </span>
              <div className="ml-auto flex items-center gap-2">
                <button className="btn-ghost" onClick={revealed !== null ? () => setRevealed(null) : reveal}>
                  {revealed !== null ? "Hide" : "Reveal"}
                </button>
                <button className="btn-ghost" onClick={() => { setEditing(true); setRevealed(null); }}>
                  Replace
                </button>
                <button className="btn-ghost" style={{ color: "var(--alarm)" }} onClick={remove}>
                  Remove
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
