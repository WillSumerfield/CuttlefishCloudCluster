import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { session } from "../app/session";
import { getArms, Unauthorized, errMessage, retractArm, returnArm, type Arm } from "../api";
import { MyArms } from "../arms/MyArms";
import { Roster } from "../arms/Roster";
import type { ArmAction } from "../arms/format";

/**
 * The fleet. My Arms (only if you own one) sits above the roster of every machine. Live — an Arm's state
 * changes as work lands on it — so it polls, like the Queue.
 *
 * Retract and return are the only two actions here, and that is the whole set the Dashboard will ever
 * have: they are owner-scoped routes on the Head, and admit/benchmark/remove are Head-CLI-only,
 * permanently (see api.ts — the Head API exposes no admin-write endpoints at all).
 */

// How each action reshapes an Arm locally — the optimistic view, until the next poll confirms it.
function applyLocal(arm: Arm, action: ArmAction): Arm {
  switch (action) {
    case "retract": return { ...arm, state: "retracted" };
    case "return": return { ...arm, state: "idle" };
  }
}

const API: Record<ArmAction, (name: string) => Promise<void>> = { retract: retractArm, return: returnArm };

export function ArmsPage() {
  const { logout } = useAuth();
  const [arms, setArms] = useState<Arm[]>([]);
  const [loaded, setLoaded] = useState(false);
  // A failed action used to revert silently on the next poll — the operator saw the row snap back with
  // no reason. Name what failed, and why, until they act again.
  const [actionError, setActionError] = useState<string | null>(null);
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
    refresh();
    timer.current = window.setInterval(refresh, 4000);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [refresh]);

  const onAct = useCallback(
    (name: string, action: ArmAction) => {
      // Optimistic: reshape the row now, then let the next poll confirm.
      setArms((prev) => prev.map((a) => (a.name === name ? applyLocal(a, action) : a)));
      setActionError(null);
      API[action](name).catch((e) => {
        if (e instanceof Unauthorized) return logout();
        // The action failed live: say what happened, then re-sync the row to the truth.
        setActionError(`Couldn't ${action} ${name} — ${errMessage(e)} The list has been refreshed.`);
        refresh();
      });
    },
    [logout, refresh],
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
      {actionError && (
        <p role="alert" className="mb-6 rounded-[10px] border px-3 py-2 text-[13px]"
           style={{ borderColor: "color-mix(in oklab, var(--alarm) 40%, transparent)", color: "var(--alarm)" }}>
          {actionError}
        </p>
      )}

      <div className="space-y-8">
        <MyArms arms={mine} onAct={onAct} />
        <Roster arms={arms} isAdmin={session.isAdmin} />
      </div>
    </Page>
  );
}
