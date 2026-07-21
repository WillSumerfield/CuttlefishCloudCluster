import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { session } from "../app/session";
import {
  getArms, Unauthorized, type Arm,
  retractArm, returnArm, admitArm, benchmarkArm, removeArm,
} from "../api";
import { MyArms } from "../arms/MyArms";
import { Roster } from "../arms/Roster";
import { demoRoster } from "../arms/mock";
import { isDemo } from "../queue/mock";
import type { ArmAction } from "../arms/format";

/**
 * The fleet. My Arms (only if you own one) sits above the roster of every machine; the Admin's powers are
 * inline on the roster's rows, never a separate section. Live — an Arm's state changes as work lands on
 * it — so it polls, like the Queue.
 *
 * The action endpoints don't exist on the Head yet, so under ?demo the actions mutate the mock in place
 * (nothing is sent); live, they call out and the next poll confirms.
 */

// How each action reshapes an Arm locally — the optimistic view, and the whole story under ?demo.
function applyLocal(arm: Arm, action: ArmAction): Arm {
  switch (action) {
    case "retract": return { ...arm, state: "retracted" };
    case "return": return { ...arm, state: "idle" };
    case "admit": return { ...arm, admitted: true, state: "idle" };
    case "benchmark": return { ...arm, state: "benchmarking" };
    case "remove": return arm; // removal drops the row (handled in onAct); nothing to reshape
  }
}

const API: Record<ArmAction, (name: string) => Promise<void>> = {
  retract: retractArm, return: returnArm, admit: admitArm, benchmark: benchmarkArm, remove: removeArm,
};

export function ArmsPage() {
  const { logout } = useAuth();
  const demo = isDemo();
  const [arms, setArms] = useState<Arm[]>(() => (demo ? demoRoster() : []));
  const [loaded, setLoaded] = useState(demo);
  const timer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      setArms(await getArms());
    } catch (e) {
      if (e instanceof Unauthorized) logout();
    } finally {
      setLoaded(true);
    }
  }, [logout]);

  useEffect(() => {
    if (demo) return; // design harness — hold the mock, don't fetch or poll.
    refresh();
    timer.current = window.setInterval(refresh, 4000);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [demo, refresh]);

  const onAct = useCallback(
    (name: string, action: ArmAction) => {
      // Optimistic: reshape (or drop) the row now, then let the next poll confirm. Under ?demo there is
      // no next poll and no endpoint — the local change IS the result.
      setArms((prev) =>
        action === "remove"
          ? prev.filter((a) => a.name !== name)
          : prev.map((a) => (a.name === name ? applyLocal(a, action) : a)),
      );
      if (demo) return;
      API[action](name).catch((e) => {
        if (e instanceof Unauthorized) logout();
        else refresh(); // an action that failed live: re-sync to the truth.
      });
    },
    [demo, logout, refresh],
  );

  const mine = useMemo(() => arms.filter((a) => a.owner === session.username), [arms]);
  const gpus = arms.reduce((n, a) => n + (a.gpus || 0), 0);

  return (
    <Page
      title="Arms"
      actions={
        loaded && arms.length > 0 ? (
          <span className="text-[13px] text-ink-mute">
            <span className="num text-ink-soft">{arms.length}</span> arm{arms.length === 1 ? "" : "s"}
            {gpus > 0 && (
              <>
                {" · "}
                <span className="num text-ink-soft">{gpus}</span> GPUs
              </>
            )}
          </span>
        ) : null
      }
    >
      {demo && (
        <p className="mb-6 rounded-[10px] px-3 py-2 text-[13px]" style={{ background: "color-mix(in oklab, var(--irid-low) 12%, transparent)", color: "var(--ink-soft)" }}>
          Demo data — not the live cluster. Drop the <span className="num">?demo</span> from the URL for the real fleet.
        </p>
      )}

      <div className="space-y-8">
        <MyArms arms={mine} onAct={onAct} />
        <Roster arms={arms} isAdmin={session.isAdmin} onAct={onAct} />
      </div>
    </Page>
  );
}
