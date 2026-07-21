import { useState } from "react";
import { changePassword, Unauthorized } from "../api";

/**
 * Voluntary password rotation — the one the app was missing (forced first-login rotation already exists in
 * the auth gate; this is change-by-choice). Collapsed to a single control until you ask for it, then a
 * current / new / confirm form. Needs the current password because you're authenticated by cookie, not
 * re-proving identity — /passwd 403s a wrong current, and that message reaches you intact.
 *
 * Under ?demo it doesn't touch the real endpoint (design review has no business changing a password); it
 * just plays the success beat.
 */
export function PasswordForm({ demo, onUnauthorized }: { demo: boolean; onUnauthorized: () => void }) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const mismatch = confirm.length > 0 && next !== confirm;
  const ready = current && next && next === confirm && !busy;

  const reset = () => {
    setCurrent(""); setNext(""); setConfirm(""); setError(null);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      if (!demo) await changePassword(current, next);
      reset();
      setOpen(false);
      setDone(true);
      window.setTimeout(() => setDone(false), 3000);
    } catch (err) {
      if (err instanceof Unauthorized) onUnauthorized();
      else setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="label">Password</span>
        {!open && (
          <button className="btn-ghost" onClick={() => { setDone(false); setOpen(true); }}>
            Change password
          </button>
        )}
      </div>

      {done && <p className="text-[13px]" style={{ color: "var(--irid-mid)" }}>Password updated.</p>}

      {open && (
        <form onSubmit={submit} className="space-y-2.5">
          <input
            className="field"
            type="password"
            autoComplete="current-password"
            placeholder="current password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
          <input
            className="field"
            type="password"
            autoComplete="new-password"
            placeholder="new password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
          <input
            className="field"
            type="password"
            autoComplete="new-password"
            placeholder="confirm new password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
          {(error || mismatch) && (
            <p className="text-[13px] text-[var(--alarm)]">{error ?? "The two passwords do not match."}</p>
          )}
          <div className="flex items-center gap-2">
            <button className="btn-primary" disabled={!ready}>{busy ? "Saving…" : "Update password"}</button>
            <button type="button" className="btn-ghost" onClick={() => { reset(); setOpen(false); }}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
