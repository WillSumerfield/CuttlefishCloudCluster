import { useState } from "react";
import type { Recipe } from "../api";
import { IridBloom } from "../queue/IridBloom";
import { CloudDrift } from "../queue/CloudDrift";
import { ago, runtime, size, vram, type RecipeAction } from "./format";

/**
 * One Recipe — a card, not a row: these are yours and few, and each carries a primary verb (Run) the roster
 * shape can't hold. A Recipe is a FROZEN snapshot, so at rest it is STILL. It moves in two cases: while a
 * fresh snapshot is uploading (`updating`) it is a wait with no known end → the chromatic bloom; while a
 * Spawn of it is live on the cluster (`running`) it is working → the passing cloud. The upload wins if both
 * are somehow true (you can't launch a snapshot that hasn't landed).
 *
 * Delete frees bytes back to the wall, so it's destructive and confirms inline. Run is the loud one — one
 * click, from any machine, nothing to hand.
 */
export function RecipeCard({
  recipe,
  onHover,
  onAct,
}: {
  recipe: Recipe;
  onHover: (name: string | null) => void;
  onAct: (name: string, action: RecipeAction) => void;
}) {
  const { name, script, args, gpus, vram_mb, minutes, size_bytes, frozen_at, updating, running } = recipe;
  const [confirm, setConfirm] = useState(false);
  const command = [script, ...args].join(" ");

  return (
    <li
      className="shard relative"
      style={{ padding: "1.9rem 1.9rem" }}
      // Hovering a card lights its own slice on the storage wall — the card itself doesn't change.
      onMouseEnter={() => onHover(name)}
      onMouseLeave={() => onHover(null)}
    >
      {updating ? <IridBloom /> : running ? <CloudDrift /> : null}

      <div className="relative z-10">
        <div className="flex items-baseline justify-between gap-2">
          <span className="num truncate text-[15px] text-ink">{name}</span>
          <span className="num shrink-0 text-[12px] text-ink-mute">{size(size_bytes)}</span>
        </div>

        {/* What it runs — the frozen command. Quiet ink, tabular, one line. */}
        <div className="num mt-1.5 truncate text-[13px] text-ink-soft">{command}</div>

        {/* Declared needs — the same ask a Spawn carries. Facts, so muted ink, never live-state colour. */}
        <div className="mt-2 flex flex-wrap items-center gap-x-3 text-[12px] text-ink-mute">
          <span className="num">{gpus} GPU{gpus === 1 ? "" : "s"}</span>
          <span aria-hidden>·</span>
          <span className="num">{vram(vram_mb)}</span>
          <span aria-hidden>·</span>
          <span className="num">{runtime(minutes)}</span>
        </div>

        {/* Status — a live word while it uploads or runs, else how stale the frozen code is. */}
        <div
          className="mt-1 text-[12px]"
          style={{ color: updating || running ? "var(--irid-dim)" : "var(--ink-mute)" }}
        >
          {updating ? "updating code…" : running ? "running now" : `frozen ${ago(frozen_at)}`}
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          {confirm ? (
            <>
              <span className="text-[12px] text-ink-mute">Frees {size(size_bytes)}. Gone for good.</span>
              <button className="btn-ghost" onClick={() => setConfirm(false)}>
                Keep
              </button>
              <button
                className="btn-ghost"
                style={{ color: "var(--alarm)" }}
                onClick={() => {
                  setConfirm(false);
                  onAct(name, "delete");
                }}
              >
                Delete
              </button>
            </>
          ) : (
            <>
              <button className="btn-primary" disabled={updating} onClick={() => onAct(name, "run")}>
                Queue
              </button>
              <button className="btn-ghost" disabled={updating} onClick={() => onAct(name, "update")}>
                Update code
              </button>
              <button className="btn-ghost" onClick={() => onAct(name, "edit")}>
                Edit
              </button>
              <button className="btn-ghost" onClick={() => setConfirm(true)}>
                Delete
              </button>
            </>
          )}
        </div>
      </div>
    </li>
  );
}
