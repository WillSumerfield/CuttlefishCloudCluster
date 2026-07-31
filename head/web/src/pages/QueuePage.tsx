import { useCallback, useEffect, useRef, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { session } from "../app/session";
import { getArms, getSpawns, Unauthorized, type Arm, type Spawn } from "../api";
import { SubmitComposer } from "../queue/SubmitComposer";
import { LiveQueue } from "../queue/LiveQueue";
import { demoArms, demoSpawns, isDemo } from "../queue/mock";

/** The landing page: submit work, and see what the cluster is doing right now. Live only. */
export function QueuePage() {
  const { logout } = useAuth();
  const demo = isDemo();
  const [spawns, setSpawns] = useState<Spawn[]>(() => (demo ? demoSpawns() : []));
  const [arms, setArms] = useState<Arm[]>(() => (demo ? demoArms() : []));
  const [loaded, setLoaded] = useState(demo);
  const timer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([getSpawns(), getArms()]);
      setSpawns(s);
      setArms(a);
    } catch (e) {
      // The session can expire while the board is open. Fall the whole app back to sign-in rather than
      // showing a board that has quietly stopped updating.
      if (e instanceof Unauthorized) logout();
    } finally {
      setLoaded(true);
    }
  }, [logout]);

  useEffect(() => {
    // ?demo — design harness, no live cluster needed. Don't fetch, don't poll; hold the mock rows.
    if (demo) return;
    refresh();
    // The queue is a live board; poll it.
    timer.current = window.setInterval(refresh, 4000);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [demo, refresh]);

  // Optimistic: drop a cancelled Spawn immediately, then let the next poll confirm it's gone.
  const onCancelled = useCallback((id: number) => {
    setSpawns((prev) => prev.filter((s) => s.id !== id));
  }, []);

  const busyGpus = spawns
    .filter((s) => /running|completing/.test(s.state))
    .reduce((n, s) => n + s.gpus, 0);
  const totalGpus = arms.reduce((n, a) => n + (a.gpus || 0), 0);

  return (
    <Page
      title="Queue"
      actions={
        loaded && totalGpus > 0 ? (
          <span className="text-[13px] text-ink-mute">
            <span className="num text-ink-soft">{busyGpus}</span> of{" "}
            <span className="num text-ink-soft">{totalGpus}</span> GPUs working
          </span>
        ) : null
      }
    >
      {demo && (
        <p className="mb-6 rounded-[10px] px-3 py-2 text-[13px]" style={{ background: "color-mix(in oklab, var(--irid-low) 12%, transparent)", color: "var(--ink-soft)" }}>
          Demo data — not the live cluster. Drop the <span className="num">?demo</span> from the URL for the real queue.
        </p>
      )}

      {/* Composer left, live queue right. Below lg they stack, composer first. The composer sticks as
          the queue scrolls, so "run something" is always in reach. */}
      <div className="grid gap-8 lg:grid-cols-[minmax(380px,500px)_1fr] lg:items-start">
        <div className="lg:sticky lg:top-6">
          <SubmitComposer
            arms={arms}
            isAdmin={session.isAdmin}
            startOpen
            onSubmitted={refresh}
          />
        </div>

        <LiveQueue spawns={spawns} onCancelled={onCancelled} />
      </div>
    </Page>
  );
}
