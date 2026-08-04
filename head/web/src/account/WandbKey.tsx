import { useCallback, useEffect, useState } from "react";
import {
  getWandbKey,
  revealWandbKey,
  setWandbKey,
  deleteWandbKey,
  Unauthorized,
  errMessage,
  type WandbKeyStatus,
} from "../api";
import { Tip } from "../app/Tip";

/**
 * The W&B key module — a stored secret, masked by default. Reveal fetches the plaintext (so you can check
 * you pasted the right one); Replace overwrites it; Remove clears it. Unset, it's an invitation to paste
 * one, with a line on what it does.
 *
 * The key is stored on the Head, per User, and injected into every spawn you submit from either client.
 * The plaintext never enters the SPA except for the one deliberate Reveal.
 */

type State = { kind: "loading" } | { kind: "status"; status: WandbKeyStatus };

export function WandbKey({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [revealed, setRevealed] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  // Reveal/Save/Remove name what failed rather than swallow it — otherwise you can't tell whether your
  // key is actually stored.
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    getWandbKey()
      .then((status) => setState({ kind: "status", status }))
      .catch((e) => {
        if (e instanceof Unauthorized) return onUnauthorized();
        setError(`Couldn't read your W&B key — ${errMessage(e)}`);
      });
  }, [onUnauthorized]);

  useEffect(() => { load(); }, [load]);

  const reveal = async () => {
    setError(null);
    try {
      setRevealed(await revealWandbKey());
    } catch (e) {
      if (e instanceof Unauthorized) return onUnauthorized();
      setError(`Couldn't reveal the stored key — ${errMessage(e)}`);
    }
  };

  const save = async () => {
    const key = draft.trim();
    if (!key) return;
    setBusy(true);
    setError(null);
    try {
      await setWandbKey(key);
      setEditing(false);
      setDraft("");
      setRevealed(null);
      load();
    } catch (e) {
      if (e instanceof Unauthorized) return onUnauthorized();
      setError(`Couldn't save your W&B key — ${errMessage(e)} It wasn't stored.`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setRevealed(null);
    setError(null);
    try {
      await deleteWandbKey();
      load();
    } catch (e) {
      if (e instanceof Unauthorized) return onUnauthorized();
      setError(`Couldn't remove the key — ${errMessage(e)}`);
    }
  };

  return (
    <div>
      {/* Label and every action on ONE row, the same header idiom PasswordForm uses — the actions belong
          to the thing named beside them, not to the value below. They light gold on hover like FactRow,
          so this module stops being the one place on the page where a control reads as bare text. */}
      <div className="mb-2 flex items-center justify-between gap-3">
        <Tip className="label" text="Held encrypted on the Head and injected into every Spawn you submit, from either client — so your runs log to your own account whichever machine you launched them from.">W&amp;B key</Tip>
        {state.kind === "status" && state.status.set && !editing && (
          <div className="flex items-center gap-2">
            <button
              className="btn-ghost btn-ghost-gold"
              onClick={revealed !== null ? () => setRevealed(null) : reveal}
            >
              {revealed !== null ? "Hide" : "Reveal"}
            </button>
            <button
              className="btn-ghost btn-ghost-gold"
              onClick={() => { setEditing(true); setRevealed(null); }}
            >
              Replace
            </button>
            <button className="btn-ghost btn-ghost-alarm" onClick={remove}>
              Remove
            </button>
          </div>
        )}
      </div>

      {error && <p role="alert" className="mb-2 text-[12px]" style={{ color: "var(--alarm)" }}>{error}</p>}

      {/* The load failing leaves this stuck on "loading" forever, so the error above replaces the
          spinner line rather than sitting beneath a promise that will never resolve. */}
      {state.kind === "loading" && !error && <p className="text-[13px] text-ink-mute">Loading…</p>}

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
            /* Just the value now — its actions moved up into the header row above. */
            <span className="num text-[13px] text-ink-soft">
              {revealed !== null ? revealed : `•••••••••• ${state.status.last4 ?? ""}`}
            </span>
          )}
        </>
      )}
    </div>
  );
}
