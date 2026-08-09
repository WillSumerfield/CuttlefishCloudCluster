import { useState } from "react";
import { type Workspace, workspaceDownloadUrl } from "../api";
import { CloudDrift } from "../queue/CloudDrift";
import { IridBloom } from "../queue/IridBloom";
import { runtime, size, visited, vram, type WorkspaceAction } from "./format";

/**
 * One Workspace — a card, like a Recipe's, and moving for the opposite reason. A Recipe is a frozen
 * thing and is STILL at rest; a Workspace is a place someone works, so it moves whenever someone is in
 * it: the passing cloud while a visit is live, the chromatic bloom in the moment between claiming the
 * visit and the cluster answering with an id.
 *
 * The fill bar is this Workspace's own **Storage** — the same fact the meter above shows as one segment,
 * repeated here because "how full am I" is the question you ask of the card in front of you. It turns
 * alarm when the tree is over its Storage, which is not a failure state so much as a stopped one: nothing
 * is evicted, the session simply stops being saved.
 *
 * Open is the loud verb. Delete confirms inline like a Recipe's, but demands the NAME typed back rather
 * than a second click — this is the only thing CCC holds with no expiry, no eviction and no second copy.
 */
export function WorkspaceCard({
  workspace,
  onHover,
  onAct,
}: {
  workspace: Workspace;
  onHover: (name: string | null) => void;
  onAct: (name: string, action: WorkspaceAction) => void;
}) {
  const { name, storage_bytes, used_bytes, gpus, vram_mb, minutes, last_visited_at, live, starting } =
    workspace;
  const [confirm, setConfirm] = useState(false);
  const [typed, setTyped] = useState("");

  const fill = Math.min(1, storage_bytes ? used_bytes / storage_bytes : 0);
  const over = used_bytes > storage_bytes;

  return (
    <div
      className="shard relative h-full"
      style={{ padding: "1.9rem 1.9rem" }}
      onMouseEnter={() => onHover(name)}
      onMouseLeave={() => onHover(null)}
    >
      {starting ? <IridBloom /> : live ? <CloudDrift /> : null}

      <div className="relative z-10">
        <div className="flex items-baseline justify-between gap-2">
          <span className="num truncate text-[15px] text-ink">{name}</span>
          <span className="num shrink-0 text-[12px] text-ink-mute">
            {size(used_bytes)} / {size(storage_bytes)}
          </span>
        </div>

        {/* This Workspace's own Storage, as its own small vessel. */}
        <div className="mt-2 h-1.5 overflow-hidden rounded-full"
             style={{ background: "color-mix(in oklab, var(--shoal) 40%, transparent)" }}>
          <div
            className="h-full"
            style={{
              width: `${fill * 100}%`,
              background: over
                ? "var(--alarm)"
                : `color-mix(in oklab, var(--irid-mid) ${Math.round(fill * 70)}%, var(--irid-dim))`,
            }}
          />
        </div>

        {/* What a visit asks for. Facts, so muted ink — never live-state colour. */}
        <div className="mt-2.5 flex flex-wrap items-center gap-x-3 text-[12px] text-ink-mute">
          <span className="num">{gpus} GPU{gpus === 1 ? "" : "s"}</span>
          <span aria-hidden>·</span>
          <span className="num">{vram(vram_mb)}</span>
          <span aria-hidden>·</span>
          <span className="num">{runtime(minutes)} per visit</span>
        </div>

        <div
          className="mt-1 text-[12px]"
          style={{ color: over ? "var(--alarm)" : live || starting ? "var(--irid-dim)" : "var(--ink-mute)" }}
        >
          {over
            ? "full — nothing is being saved"
            : starting
              ? "starting a visit…"
              : live
                ? `open now — spawn ${live}`
                : visited(last_visited_at)}
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          {confirm ? (
            <div className="w-full space-y-2">
              <p className="text-[12px] text-ink-mute">
                Frees {size(storage_bytes)}. There is no other copy of this — type{" "}
                <span className="num text-ink-soft">{name}</span> to confirm.
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  className="field min-w-[120px] flex-1"
                  value={typed}
                  onChange={(e) => setTyped(e.target.value)}
                  aria-label={`Type ${name} to confirm deletion`}
                  autoFocus
                />
                <button className="btn-ghost" onClick={() => { setConfirm(false); setTyped(""); }}>
                  Keep
                </button>
                <button
                  className="btn-ghost"
                  style={{ color: "var(--alarm)" }}
                  disabled={typed !== name}
                  onClick={() => { setConfirm(false); setTyped(""); onAct(name, "delete"); }}
                >
                  Delete
                </button>
              </div>
            </div>
          ) : (
            <>
              <button className="btn-primary" disabled={starting} onClick={() => onAct(name, "open")}>
                {live ? "Attach" : "Open"}
              </button>
              <button className="btn-ghost" onClick={() => onAct(name, "edit")}>
                Edit
              </button>
              {/* A plain link, not a fetch: the Head streams the tar, and a Storage runs to 16 GB and up —
                  this belongs to the browser's download manager, not to this tab's memory. */}
              <a className="btn-ghost" href={workspaceDownloadUrl(name)} download>
                Download
              </a>
              {/* Refused by the Head while a visit is live (the tree is what that session mirrors into),
                  so it is not offered here either. */}
              <button className="btn-ghost" disabled={!!live || starting} onClick={() => setConfirm(true)}>
                Delete
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
