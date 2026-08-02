import type { Recipe } from "../api";
import { ALLOWANCE, size } from "./format";

/**
 * MY STORAGE — the signature of this page, and the one thing on it that is a wall. The 24 GB allowance is
 * a vessel; your Recipes fill it left to right as a single lit iridescent mass (storage is a Head fact, so
 * it is iridescence, never pigment). The fill's chroma rises as it fills — cool and faint when there's room,
 * deeper cyan as it nears the wall — because on the iridescence ramp chroma IS "how much". There are no rules
 * inside the mass: the leading edge dissolves into the empty allowance rather than ending on a hard line.
 *
 * The Recipes ARE the fill, so the two are linked: hovering a card lights that Recipe's own slice of the
 * mass brighter, and hovering the meter is answered by its card. That is the whole point of a hard wall you
 * must delete from — you can see which Recipe is eating the room.
 *
 * `pending` is the composer talking to the wall: the bytes of a folder being frozen right now, previewed as
 * a ghost slice past the committed fill. If it fits, the ghost is faint iridescence dissolving into the free
 * room; if it would breach the wall, the ghost turns to --alarm and slams flush against the right edge —
 * you can see it hit the wall before you spend the upload.
 */
export function Storage({
  recipes,
  hovered,
  pending = 0,
}: {
  recipes: Recipe[];
  hovered: string | null;
  pending?: number;
}) {
  const used = recipes.reduce((n, r) => n + r.size_bytes, 0);
  const usedFrac = Math.min(1, used / ALLOWANCE);
  const free = Math.max(0, ALLOWANCE - used);

  // Cumulative offsets, in allowance fractions, so each Recipe owns a contiguous slice [start, start+w).
  let acc = 0;
  const slices = recipes.map((r) => {
    const start = acc / ALLOWANCE;
    const w = r.size_bytes / ALLOWANCE;
    acc += r.size_bytes;
    return { name: r.name, start, w };
  });
  const litIdx = slices.findIndex((s) => s.name === hovered);
  const lit = litIdx >= 0 ? slices[litIdx] : undefined;
  // Feather only the SEAMS the slice shares with an adjacent recipe (gold dissolving into the neighbour's
  // blue). An outer end — the first slice's left, the last slice's right — is the mass's own boundary, not a
  // seam, so it runs solid to the edge and lets the pill's rounded cap (and the leading feather) show through.
  const litLeft = litIdx <= 0 ? "#000 0" : "transparent 0, #000 10px";
  const litRight = litIdx === slices.length - 1 ? "#000 100%" : "#000 calc(100% - 10px), transparent 100%";
  const litMask = `linear-gradient(90deg, ${litLeft}, ${litRight})`;

  // The pending folder, as a ghost past the committed fill. Over the wall → it can't render past 100%, so
  // it fills the gap to the wall and turns alarm; the composer carries the "X over" number in words.
  const pendingFrac = pending / ALLOWANCE;
  const over = usedFrac + pendingFrac > 1.0001;
  const ghostStart = usedFrac;
  const ghostW = Math.max(0, Math.min(1, usedFrac + pendingFrac) - ghostStart);

  // Fuller → more chroma: irid-dim (barely coloured, room to spare) shading toward irid-mid (contended cyan)
  // as the wall fills. The mass is one gradient; the rise is in the mix, not in any internal division.
  const fill = `color-mix(in oklab, var(--irid-mid) ${Math.round(usedFrac * 70)}%, var(--irid-dim))`;

  return (
    <section>
      <h2 className="label mb-3 pl-1">Storage</h2>
      <div className="pl-1">
        <div className="mb-3 flex items-baseline justify-between">
          <span className="text-[14px] text-ink-soft">
            <span className="num text-ink">{size(used)}</span> of{" "}
            <span className="num">{size(ALLOWANCE)}</span>
          </span>
          <span className="num text-[13px] text-ink-mute">{size(free)} free</span>
        </div>

        {/* The wall: the fill rises across it; its leading edge is feathered (mask) so the mass dissolves
            into the free allowance instead of stopping on a drawn line. No vessel behind it and no hover of
            its own — it only ever answers a card. A slight blur feathers the whole PILL PERIMETER at once —
            the true rounded outline, softened uniformly — rather than two independent x/y ramps that leave
            the corners sharp (same reason the drop strip feathers with a blur, not the surface mask). */}
        <div
          className="relative h-4 overflow-hidden rounded-full"
          style={{ filter: "blur(1.5px)" }}
        >
          <div
            className="absolute inset-y-0 left-0"
            style={{
              width: `${usedFrac * 100}%`,
              background: fill,
              WebkitMaskImage: "linear-gradient(90deg, #000 calc(100% - 14px), transparent)",
              maskImage: "linear-gradient(90deg, #000 calc(100% - 14px), transparent)",
            }}
          />

          {/* The hovered Recipe's own slice, turned GOLD — pure gold across the slice, its edges feathered
              to transparent so the gold dissolves into the neighbouring blue at the seams (the blue fill
              shows through underneath). The link between a card and the wall it fills. */}
          {lit && (
            <div
              aria-hidden
              className="absolute inset-y-0"
              style={{
                left: `${lit.start * 100}%`,
                width: `${lit.w * 100}%`,
                background: "var(--gold)",
                WebkitMaskImage: litMask,
                maskImage: litMask,
              }}
            />
          )}

          {/* The pending folder — a ghost past the committed fill. Feathered into free room when it fits;
              alarm and flush to the wall when it would breach. */}
          {ghostW > 0 && (
            <div
              aria-hidden
              className="absolute inset-y-0"
              style={{
                left: `${ghostStart * 100}%`,
                width: `${ghostW * 100}%`,
                background: over
                  ? "color-mix(in oklab, var(--alarm) 55%, transparent)"
                  : "color-mix(in oklab, var(--irid-low) 50%, transparent)",
                WebkitMaskImage: over ? undefined : "linear-gradient(90deg, #000 calc(100% - 12px), transparent)",
                maskImage: over ? undefined : "linear-gradient(90deg, #000 calc(100% - 12px), transparent)",
              }}
            />
          )}
        </div>
      </div>
    </section>
  );
}
