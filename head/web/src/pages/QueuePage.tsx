import { useCallback, useEffect, useRef, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { getArms, getDives, Unauthorized, type Arm, type Dive } from "../api";
import { LiveQueue } from "../queue/LiveQueue";
import { DiveComposer } from "../queue/DiveComposer";

/** Start a Dive beside the live cluster queue. */
export function QueuePage() {
  const { logout } = useAuth();
  const [dives, setDives] = useState<Dive[]>([]);
  const [arms, setArms] = useState<Arm[]>([]);
  const [loaded, setLoaded] = useState(false);
  const timer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([getDives(), getArms()]);
      setDives(s);
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
    refresh();
    // The queue is a live board; poll it.
    timer.current = window.setInterval(refresh, 4000);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [refresh]);

  // Optimistic: drop a cancelled Dive immediately, then let the next poll confirm it's gone.
  const onCancelled = useCallback((id: number) => {
    setDives((prev) => prev.filter((s) => s.id !== id));
  }, []);

  const busyGpus = dives
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
      <div className="grid gap-8 xl:grid-cols-[minmax(320px,400px)_minmax(0,1fr)] xl:items-start">
        <DiveComposer />
        <div className="min-w-0"><LiveQueue dives={dives} onCancelled={onCancelled} /></div>
      </div>
    </Page>
  );
}
