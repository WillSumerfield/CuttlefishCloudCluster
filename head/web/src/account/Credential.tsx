import { useCallback, useEffect, useState } from "react";
import {
  getCredential,
  setCredential,
  deleteCredential,
  Unauthorized,
  errMessage,
  type CredentialKind,
  type CredentialStatus,
} from "../api";
import { Tip } from "../app/Tip";

/**
 * One **Credential** module — a stored third-party secret, kept under the User's own password on the
 * Head and injected into every Spawn they submit from either client.
 *
 * **Write-only, and this component is the shape of that.** There is no Reveal: the plaintext leaves
 * this page once, when it is pasted, and never comes back. What is shown afterwards is the `hint` the
 * Head keeps in the clear beside it — for a W&B key, its last 4 characters — which answers the only
 * question Reveal was really answering ("is this the key I meant?") without putting the secret on the
 * wire, in scrollback, or on the screen of whoever is standing behind you.
 *
 * Parameterised by `kind` rather than written per secret, mirroring `credentials.KINDS` on the Head:
 * adding the GitHub SSH key should be an entry in SPECS below, not a second copy of this file.
 */

type Spec = {
  label: string;
  /** What the Tip beside the label explains — why the Head is holding this at all. */
  tip: string;
  placeholder: string;
  /** The line shown when nothing is stored yet. */
  invitation: string;
  /** How the hint reads back. A W&B key is one opaque string, so a masked run + its tail. */
  masked: (hint: string | null) => string;
};

const SPECS: Record<CredentialKind, Spec> = {
  wandb: {
    label: "W&B key",
    tip: "Held on the Head under your password and injected into every Spawn you submit, from either client — so your runs log to your own account whichever machine you launched them from. Nobody can read it back, including you.",
    placeholder: "paste your W&B API key",
    invitation: "Injected into every spawn you submit, from either client. Held under your password — nobody can read it back.",
    masked: (hint) => `•••••••••• ${hint ?? ""}`,
  },
};

type State = { kind: "loading" } | { kind: "status"; status: CredentialStatus };

export function Credential({
  kind,
  onUnauthorized,
}: {
  kind: CredentialKind;
  onUnauthorized: () => void;
}) {
  const spec = SPECS[kind];
  const [state, setState] = useState<State>({ kind: "loading" });
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  // Save/Remove name what failed rather than swallow it — otherwise you can't tell whether your
  // key is actually stored.
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    getCredential(kind)
      .then((status) => setState({ kind: "status", status }))
      .catch((e) => {
        if (e instanceof Unauthorized) return onUnauthorized();
        setError(`Couldn't read your ${spec.label} — ${errMessage(e)}`);
      });
  }, [kind, spec.label, onUnauthorized]);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    const value = draft.trim();
    if (!value) return;
    setBusy(true);
    setError(null);
    try {
      await setCredential(kind, value);
      setEditing(false);
      setDraft("");
      load();
    } catch (e) {
      if (e instanceof Unauthorized) return onUnauthorized();
      setError(`Couldn't save your ${spec.label} — ${errMessage(e)} It wasn't stored.`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setError(null);
    try {
      await deleteCredential(kind);
      load();
    } catch (e) {
      if (e instanceof Unauthorized) return onUnauthorized();
      setError(`Couldn't remove the ${spec.label} — ${errMessage(e)}`);
    }
  };

  return (
    <div>
      {/* Label and every action on ONE row, the same header idiom PasswordForm uses — the actions belong
          to the thing named beside them, not to the value below. They light gold on hover like FactRow,
          so this module stops being the one place on the page where a control reads as bare text. */}
      <div className="mb-2 flex items-center justify-between gap-3">
        <Tip className="label" text={spec.tip}>{spec.label}</Tip>
        {state.kind === "status" && state.status.set && !editing && (
          <div className="flex items-center gap-2">
            <button className="btn-ghost btn-ghost-gold" onClick={() => setEditing(true)}>
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
                  placeholder={spec.placeholder}
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
                <p className="text-[12px] text-ink-mute">{spec.invitation}</p>
              )}
            </div>
          ) : (
            /* Just the hint now — its actions moved up into the header row above, and there is no
               third one: Reveal is gone because the Head has nothing to reveal. */
            <span className="num text-[13px] text-ink-soft">{spec.masked(state.status.hint)}</span>
          )}
        </>
      )}
    </div>
  );
}
