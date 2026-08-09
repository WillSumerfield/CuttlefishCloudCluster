import type { Workspace } from "../api";
import { ALLOWANCE, size } from "./format";

/**
 * MY SPACE — the same vessel as the Recipe wall, showing a different KIND of thing, and the difference
 * is the whole point of the page.
 *
 * A Recipe shelf is one mass that grows until it hits a wall. This allowance is DIVIDED: each Workspace
 * reserves a Storage out of it when it is created, and what a session runs out of is that one Workspace's
 * Storage, never a pool shared with the rest. So the meter is drawn as segments with gaps between them —
 * separate vessels sitting in one allowance — rather than a continuous fill.
 *
 * Inside each segment, the darker inner bar is what that Workspace actually HOLDS. Reserved-but-empty
 * reads as an outline with nothing in it, which is exactly what it is: room its owner has already spent
 * from the pool and is not using. That is the fact the page exists to make legible, because it is the
 * one only they can fix.
 *
 * Hovering a card lights its own segment gold, the same link the Recipe wall makes.
 */
export function Storage({
  workspaces,
  hovered,
  pending = 0,
}: {
  workspaces: Workspace[];
  hovered: string | null;
  /** A Storage the composer is about to reserve, previewed past the committed segments. */
  pending?: number;
}) {
  const reserved = workspaces.reduce((n, w) => n + w.storage_bytes, 0);
  const held = workspaces.reduce((n, w) => n + w.used_bytes, 0);
  const free = Math.max(0, ALLOWANCE - reserved);

  let acc = 0;
  const segments = workspaces.map((w) => {
    const start = acc / ALLOWANCE;
    const width = w.storage_bytes / ALLOWANCE;
    acc += w.storage_bytes;
    // Guarded: a Workspace can be over its Storage (the mirror refuses to sync, it does not truncate),
    // and a fill wider than its own segment would run into the neighbour's.
    const fill = Math.min(1, w.storage_bytes ? w.used_bytes / w.storage_bytes : 0);
    return { name: w.name, start, width, fill, over: w.used_bytes > w.storage_bytes };
  });

  const pendingFrac = pending / ALLOWANCE;
  const overPool = reserved + pending > ALLOWANCE * 1.0001;
  const ghostStart = Math.min(1, reserved / ALLOWANCE);
  const ghostW = Math.max(0, Math.min(1, (reserved + pending) / ALLOWANCE) - ghostStart);

  return (
    <section>
      <h2 className="label mb-3 pl-1">Storage</h2>
      <div className="pl-1">
        <div className="mb-3 flex items-baseline justify-between">
          <span className="text-[14px] text-ink-soft">
            <span className="num text-ink">{size(reserved)}</span> of{" "}
            <span className="num">{size(ALLOWANCE)}</span> reserved
          </span>
          <span className="num text-[13px] text-ink-mute">
            {size(held)} held · {size(free)} free
          </span>
        </div>

        <div className="relative h-4 overflow-hidden rounded-full">
          {segments.map((s) => {
            const lit = s.name === hovered;
            return (
              <div
                key={s.name}
                aria-hidden
                className="absolute inset-y-0 overflow-hidden rounded-full"
                style={{
                  // The gap is drawn by inset, not by a border: nothing in this interface has a rim.
                  left: `calc(${s.start * 100}% + 1px)`,
                  width: `calc(${s.width * 100}% - 2px)`,
                  // The reservation itself — faint, because reserved-and-empty is room, not content.
                  background: "color-mix(in oklab, var(--irid-dim) 30%, transparent)",
                }}
              >
                {/* What it actually holds, inside its own vessel. */}
                <div
                  className="absolute inset-y-0 left-0"
                  style={{
                    width: `${s.fill * 100}%`,
                    background: s.over
                      ? "var(--alarm)"
                      : lit
                        ? "var(--gold)"
                        : `color-mix(in oklab, var(--irid-mid) ${Math.round(s.fill * 70)}%, var(--irid-dim))`,
                  }}
                />
                {/* Hovering the card lights the whole vessel, not just its fill, so an EMPTY workspace
                    still answers — the empty ones are the ones worth finding. */}
                {lit && (
                  <div
                    className="absolute inset-0"
                    style={{ background: "color-mix(in oklab, var(--gold) 22%, transparent)" }}
                  />
                )}
              </div>
            );
          })}

          {ghostW > 0 && (
            <div
              aria-hidden
              className="absolute inset-y-0 rounded-full"
              style={{
                left: `calc(${ghostStart * 100}% + 1px)`,
                width: `calc(${ghostW * 100}% - 2px)`,
                background: overPool
                  ? "color-mix(in oklab, var(--alarm) 55%, transparent)"
                  : "color-mix(in oklab, var(--irid-low) 40%, transparent)",
              }}
            />
          )}
        </div>
      </div>
    </section>
  );
}
