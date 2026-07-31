import { useEffect, useState } from "react";
import { Page } from "../app/Page";
import { Placeholder } from "./Placeholder";
import { downloadRecovery, getRecovery, type RecoveryStatus } from "../api";
import { human } from "../project";

/**
 * Recovery Window (docs/TODO.md): a 30-min, 16GB-capped safety net for whatever this Spawn wrote
 * OUTSIDE its declared $CCC_OUT. Self-contained on purpose — the rest of this page is still the
 * unbuilt Placeholder below (verdict/logs/outputs, separate pre-existing work), but a User who
 * mis-declared --output needs this to actually work today, not once that page exists.
 *
 * Renders nothing while loading, and nothing once there's nothing to show (no copy was ever made, or
 * the window has closed) — there is no "expired" state worth a User's attention after the fact.
 */
function RecoveryPanel({ id }: { id: string }) {
  const jobId = Number(id);
  const [status, setStatus] = useState<RecoveryStatus | null>(null);
  const [left, setLeft] = useState<number | null>(null);

  useEffect(() => {
    if (!Number.isFinite(jobId)) return;
    let alive = true;
    getRecovery(jobId).then((st) => alive && setStatus(st));
    return () => {
      alive = false;
    };
  }, [jobId]);

  useEffect(() => {
    if (!status) {
      setLeft(null);
      return;
    }
    const tick = () =>
      setLeft(Math.max(0, Math.round((new Date(status.expires_at).getTime() - Date.now()) / 60_000)));
    tick();
    const timer = setInterval(tick, 15_000); // coarse — this is a courtesy countdown, not a live clock
    return () => clearInterval(timer);
  }, [status]);

  if (!status || left === null || left <= 0) return null;

  return (
    <div
      className="surface mb-4 flex flex-wrap items-center justify-between gap-3"
      style={{ padding: "var(--pad)" }}
    >
      <div>
        <p className="text-[14px] text-ink">
          Recovery Window open — <span className="num">{left}m</span> left
        </p>
        <p className="mt-0.5 text-[12px] text-ink-mute">
          <span className="num">{human(status.bytes)}</span> this Spawn wrote outside its declared
          outputs, held on the Head as a safety net.
        </p>
      </div>
      <button
        className="rounded-[var(--radius-control)] px-4 py-2 text-[13px]"
        style={{ background: "var(--shoal)", color: "var(--ink)" }}
        onClick={() => downloadRecovery(jobId)}
      >
        Download recovery copy
      </button>
    </div>
  );
}

/** One Spawn, reached from Queue or History. Not a rail destination. */
export function SpawnPage({ id }: { id: string }) {
  return (
    <Page title={id}>
      <RecoveryPanel id={id} />
      <Placeholder what="What this Spawn did: its verdict, its logs (stdout and stderr, separately), its outputs, and what it cost." />
    </Page>
  );
}
