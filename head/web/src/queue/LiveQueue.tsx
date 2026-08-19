import type { Spawn } from "../api";
import { session } from "../app/session";
import { SpawnRow } from "./SpawnRow";

/**
 * The live queue, split into two groups because the split is the truth: Running work and Waiting work
 * are different states, and they live in different places rather than sharing a table with a status
 * column. Running is unordered; Waiting is numbered, because its order is real information.
 */

function isRunning(s: Spawn): boolean {
  // squeue state comes joined with '+', e.g. "running" or "pending". Running/configuring count as live
  // work; everything else is still waiting for a machine.
  return /running|completing|configuring/.test(s.state);
}

function Group({
  title,
  spawns,
  variant,
  empty,
  onCancelled,
}: {
  title: string;
  spawns: Spawn[];
  variant: "running" | "waiting";
  empty: string;
  onCancelled: (id: number) => void;
}) {
  // Always rendered (never null): the two groups sit side by side, so an empty column still needs its
  // header or the grid goes lopsided and the surviving column reads as "the whole queue".
  return (
    <section>
      <div className="mb-3 flex items-baseline gap-2 pl-12 pr-1">
        <h2 className="label">{title}</h2>
        <span className="num text-[12px] text-ink-mute">{spawns.length}</span>
      </div>
      {spawns.length === 0 ? (
        <p className="px-1 text-[13px] text-ink-mute">{empty}</p>
      ) : (
        <ul className="space-y-2.5">
          {spawns.map((s, i) => (
            <SpawnRow
              key={s.id}
              spawn={s}
              variant={variant}
              position={variant === "waiting" ? i + 1 : undefined}
              // Your own, and only your own. An Admin may cancel anyone's — but not from here:
              // DELETE /spawns/{id} has no admin branch (the Head API has no admin writes at all), so
              // offering the button to an admin only ever produced a red refusal. Anyone else's is
              // `cuttle cancel <id>` on the Head. See docs/Security.md.
              canCancel={s.submitted_by === session.username}
              onCancelled={onCancelled}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

export function LiveQueue({
  spawns,
  onCancelled,
}: {
  spawns: Spawn[];
  onCancelled: (id: number) => void;
}) {
  const running = spawns.filter(isRunning);
  const waiting = spawns.filter((s) => !isRunning(s));

  return (
    <div className="grid gap-x-8 gap-y-8 sm:grid-cols-2">
      <Group
        title="Running"
        spawns={running}
        variant="running"
        empty=""
        onCancelled={onCancelled}
      />
      <Group
        title="Waiting"
        spawns={waiting}
        variant="waiting"
        empty=""
        onCancelled={onCancelled}
      />
    </div>
  );
}
