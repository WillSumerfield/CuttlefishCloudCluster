import { useState } from "react";
import type { Spawn } from "../api";
import { Link } from "../app/router";
import { duration, armLabel } from "./format";
import { IridBloom } from "./IridBloom";
import { CloudDrift } from "./CloudDrift";
import { CancelSpawn } from "./CancelSpawn";

/**
 * One Spawn in the live queue. Two variants, and the difference between them is the whole point of the
 * page — it is carried by STRUCTURE and MOTION, never by a status pill:
 *
 *   running — the passing cloud drifts across it (dark bands = this machine is working). It shows the
 *             Arm it landed on. Unordered: running is not a sequence.
 *   waiting — the chromatic bloom washes through it (cool iridescence = waiting, no known end). It
 *             shows its queue position and declared duration. It has no Arm yet.
 *
 * Attribution (who submitted) is plain ink, never pigment — pigment means karma and nothing else.
 *
 * The whole row opens the Spawn (docs/Dashboard.md: "Click a Spawn anywhere"), which History has always
 * honoured and this did not. It is a STRETCHED `<a>` — absolutely positioned over the row — rather than a
 * wrapper, because this row has a button in it and History does not. A wrapper would nest a `<button>`
 * inside an `<a>` (invalid, and one click would both cancel and navigate); an onClick on the `<li>` would
 * work but stop being a real link, losing middle-click and new-tab. The overlay keeps both: Cancel sits
 * ABOVE it on z-30 and swallows its own clicks, so no stopPropagation is needed anywhere.
 *
 * That last part only works because Cancel is a child of the `<li>`, which is why the `<li>` is the flex
 * row rather than a wrapper inside it. Cancel used to live in the `relative z-10` content lift, and a
 * numeric z-index on a positioned element opens a STACKING CONTEXT — so its z-30 ranked only against its
 * siblings inside that lift, and the whole lift (Cancel included) painted at z-10, under the overlay. The
 * button was unclickable: every press landed on the link. Ordering across the two therefore has to happen
 * in the `<li>`'s context, where 30 genuinely beats 20.
 *
 * The overlay is also dropped entirely while a confirmation is open, so a click that misses Keep/Cancel
 * spawn does nothing rather than navigating away and discarding the question.
 */

export function SpawnRow({
  spawn,
  variant,
  position,
  canCancel,
  onCancelled,
}: {
  spawn: Spawn;
  variant: "running" | "waiting";
  position?: number;
  canCancel: boolean;
  onCancelled: (id: number) => void;
}) {
  const [confirming, setConfirming] = useState(false);

  const running = variant === "running";

  return (
    // Each variant carries its own motion layer behind the content: running → `CloudDrift`, discrete dark
    // bands drifting across (this machine is working); waiting → `IridBloom`, a screen-blended WebGL current
    // (waiting, no known end). Content lives in an inner lifted div so it sits above whichever layer the card
    // carries. `shard` carries the plate + sink/fade/radius/padding.
    <li className="shard queue-row relative flex items-center gap-4">
      {running ? <CloudDrift /> : <IridBloom />}
      {/* Above the motion layers and the content (z-10), below the Cancel cluster (z-30). Labelled
          because it has no text of its own — the row's name is painted by the div beside it. */}
      {!confirming && (
        <Link
          to={`/spawn/${spawn.id}`}
          className="absolute inset-0 z-20"
          aria-label={`Open spawn ${spawn.id}`}
        />
      )}
      <div className="relative z-10 flex min-w-0 flex-1 items-center gap-4">
      {/* Position — waiting only. The one place a number is structural: it IS the queue. */}
      {variant === "waiting" && (
        <span
          className="num w-6 shrink-0 text-center text-[15px] text-ink-mute"
          style={{ fontWeight: 500 }}
          aria-label={`position ${position}`}
        >
          {position}
        </span>
      )}

      {/* Running gets a faint live tick — mild iridescence, the amendment to "state has no colour". */}
      {variant === "running" && (
        <span
          aria-hidden
          className="h-2 w-2 shrink-0 rounded-full"
          style={{
            background: "var(--irid-dim)",
            boxShadow: "0 0 10px 1px color-mix(in oklab, var(--irid-dim) 60%, transparent)",
          }}
        />
      )}

      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="num truncate text-[15px] text-ink">{spawn.name || `spawn ${spawn.id}`}</span>
          <span className="num shrink-0 text-[12px] text-ink-mute">#{spawn.id}</span>
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px] text-ink-mute">
          {spawn.submitted_by && <span className="text-ink-soft">{spawn.submitted_by}</span>}
          <span>
            {spawn.gpus} GPU{spawn.gpus === 1 ? "" : "s"}
          </span>
          {variant === "running" ? (
            <>
              <span aria-hidden>·</span>
              <span className="num">{armLabel(spawn.node)}</span>
            </>
          ) : (
            <>
              <span aria-hidden>·</span>
              <span className="num">{duration(spawn.time_limit)}</span>
              {/* No "building environment" here any more. A Spawn used to sit in Waiting while the Head
                  built its Python — not running, but not waiting for a machine either — and this said
                  so. The Arm builds it inside its own allocation now, so such a Spawn is Running and
                  the queue has nothing left to explain. */}
            </>
          )}
        </div>
      </div>
      </div>

      {/* Sibling of the overlay, not of the content — see the note above. */}
      {canCancel && (
        <CancelSpawn
          id={spawn.id}
          confirming={confirming}
          setConfirming={setConfirming}
          onCancelled={onCancelled}
          className="relative z-30 shrink-0"
        />
      )}
    </li>
  );
}
