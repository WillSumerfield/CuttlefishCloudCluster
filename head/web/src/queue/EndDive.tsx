import { useState } from "react";
import { endDive, errMessage } from "../api";

/**
 * Cancel one outstanding Dive. The confirmation is INLINE, never a browser dialog, and it names the
 * consequence rather than asking "are you sure?" — a Cancel settles for the seconds actually held.
 *
 * Shared by the queue row and the Dive page, which is why `confirming` is CONTROLLED: the row suppresses
 * its own stretched link while a confirmation is open (a stray click would otherwise navigate to the Dive
 * and silently discard it), so the one flag with two readers lives with the caller that has them. `busy`
 * and `error` are nobody else's business and stay here.
 *
 * It resets itself on success. The queue row unmounts the instant its Dive leaves the list and would
 * never notice; the Dive page stays mounted and would otherwise sit on "Cancelling…" for good.
 */
export function EndDive({
  id,
  confirming,
  setConfirming,
  onCancelled,
  className,
}: {
  id: number;
  confirming: boolean;
  setConfirming: (v: boolean) => void;
  onCancelled: (id: number) => void;
  className?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const doCancel = async () => {
    setBusy(true);
    setError(null);
    try {
      await endDive(id);
      onCancelled(id);
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <div className={className}>
      {!confirming ? (
        <button className="btn-ghost" onClick={() => setConfirming(true)} disabled={busy}>
          End
        </button>
      ) : (
        <div className="flex items-center gap-2">
          <span className="hidden text-[12px] text-ink-mute sm:inline">Final mirror, then settles.</span>
          <button className="btn-ghost" onClick={() => setConfirming(false)} disabled={busy}>
            Keep
          </button>
          <button
            className="btn-ghost"
            onClick={doCancel}
            disabled={busy}
            style={{ color: "var(--alarm)" }}
          >
            {busy ? "Ending…" : "End dive"}
          </button>
        </div>
      )}
      {error && (
        <p className="mt-1 max-w-[18rem] text-right text-[12px] text-[var(--alarm)]">{error}</p>
      )}
    </div>
  );
}
