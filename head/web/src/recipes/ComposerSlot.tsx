import { useEffect, useState } from "react";
import { COMPOSER_FADE, COMPOSER_RADIUS } from "../app/Composer";

// Everything else in this app transitions in 140-160ms; this is a much bigger move (a ~230px card
// becoming a ~1000px+ composer), so it gets its own slower pace.
const GROW_MS = 350;
const FADE_MS = 150;
// Old content clears out of the way fast — a quarter of the grow — so the colour crossfade (which still
// runs the full GROW_MS on the backdrop, see below) is doing most of its own work in the open, rather
// than being hidden under still-opaque old content for most of the animation.
const CLOSED_FADE_MS = Math.round(GROW_MS / 4);

// Generous fixed caps, not a measured target height (the Composer's real height varies with its content
// — feature chips, an error line — and there's no single right answer). Picked comfortably
// above what either side ever naturally renders at, so overflow:hidden never actually clips anything —
// the animation is purely a max-height tween between two constants, no JS measurement involved.
const CLOSED_CAP = 230;
const OPEN_CAP = 1800;

// A flat approximation of `.shard`'s own gradient (index.css) — a lit-top-to-body blend, roughly its
// prevailing tone. `open` is always the recipe Composer, so this one tint covers every call site; the
// real gradient (drawn on `open`'s own ::before) fades in on top of it, so by the time it's visible the
// colour underneath is already close and the handoff doesn't pop.
const OPEN_TINT = "color-mix(in oklab, var(--shoal) 20%, var(--body))";

// Same crossed-linear-ramp edge dissolve every surface in this app uses (index.css's `.surface`,
// NewRecipeTile's own recess fill), at COMPOSER_FADE — the recipe composer's own fade depth (Composer.tsx)
// — rather than a separate hardcoded number, so the "alpha border" during growing/shrinking matches the
// settled card exactly instead of just approximating it. Without a mask at all the backdrop would be a
// hard-edged rectangle, and "no border — dissolve at every edge" (the app's own rule) would stop applying
// for exactly the two phases where the backdrop is the only thing visible behind the content.
const backdropRamp = (dir: string) =>
  `linear-gradient(${dir},` +
  ` transparent 0,` +
  ` rgba(0,0,0,.156) ${COMPOSER_FADE * 0.5}px,` +
  ` rgba(0,0,0,.5) ${COMPOSER_FADE}px,` +
  ` rgba(0,0,0,.844) ${COMPOSER_FADE * 1.5}px,` +
  ` #000 ${COMPOSER_FADE * 2}px,` +
  ` #000 calc(100% - ${COMPOSER_FADE * 2}px),` +
  ` rgba(0,0,0,.844) calc(100% - ${COMPOSER_FADE * 1.5}px),` +
  ` rgba(0,0,0,.5) calc(100% - ${COMPOSER_FADE}px),` +
  ` rgba(0,0,0,.156) calc(100% - ${COMPOSER_FADE * 0.5}px),` +
  ` transparent 100%)`;
const BACKDROP_MASK = `${backdropRamp("to right")}, ${backdropRamp("to bottom")}`;

type Phase = "closed" | "growing" | "open" | "closing" | "shrinking";

/**
 * The shared "becomes a composer" shell — used both for the New-recipe tile and for a RecipeCard's own
 * Update action. One persistent <li> (the actual grid item) so the box can grow instead of an instant
 * unmount/remount. `open` only mounts once a grow starts, so an idle shelf of many cards never fires the
 * Composer's own fetches (feature catalog) once per card.
 *
 * Opening: shell starts growing (GROW_MS) and `closed` fades out quickly alongside it (CLOSED_FADE_MS,
 * a quarter of the grow — old content clears out of the way early rather than sitting opaque over most
 * of the animation), then once fully grown `open` fades in (FADE_MS). Closing is the exact reverse
 * ORDER, not just reversed timing: `open` fades out first (FADE_MS), then the shell shrinks and `closed`
 * fades back in together (GROW_MS, unchanged — only the way OUT is quickened).
 *
 * A flip mid-animation jumps straight to the other end rather than reversing partway through — simpler,
 * and "opened then immediately cancelled" is rare enough not to need a smooth mid-flight reversal (CSS
 * transitions still interrupt gracefully from wherever they currently are, so it never actually snaps).
 */
export function ComposerSlot({
  isOpen,
  closed,
  open,
  closedTint = OPEN_TINT,
}: {
  isOpen: boolean;
  closed: React.ReactNode;
  open: React.ReactNode;
  /** `closed`'s own prevailing colour, so the backdrop can crossfade into it rather than popping in blue
   * underneath still-opaque content. Defaults to the same shard-ish tone as `open` (fine for a RecipeCard,
   * which is already a shard) — the New-recipe tile overrides it with its own much darker recess colour. */
  closedTint?: string;
}) {
  const [phase, setPhase] = useState<Phase>(isOpen ? "open" : "closed");

  useEffect(() => {
    setPhase((p) => {
      if (isOpen) return p === "closed" ? "growing" : p === "closing" || p === "shrinking" ? "open" : p;
      return p === "open" ? "closing" : p === "growing" ? "closed" : p;
    });
  }, [isOpen]);

  // Multi-step phases advance themselves on a timer.
  useEffect(() => {
    if (phase === "growing") {
      const t = setTimeout(() => setPhase((p) => (p === "growing" ? "open" : p)), GROW_MS);
      return () => clearTimeout(t);
    }
    if (phase === "closing") {
      const t = setTimeout(() => setPhase((p) => (p === "closing" ? "shrinking" : p)), FADE_MS);
      return () => clearTimeout(t);
    }
    if (phase === "shrinking") {
      const t = setTimeout(() => setPhase((p) => (p === "shrinking" ? "closed" : p)), GROW_MS);
      return () => clearTimeout(t);
    }
  }, [phase]);

  const composerMounted = phase !== "closed";
  const grown = phase === "growing" || phase === "open" || phase === "closing";
  const closedVisible = phase === "closed" || phase === "shrinking";
  const composerVisible = phase === "open";
  // Visible only while the size is actually moving — that's the one moment neither real layer is both
  // present and fully opaque, so there'd otherwise be nothing behind the growing/shrinking box. Once
  // settled (open, or fading out during closing) the mounted content is fully opaque on its own and
  // should dissolve into the real page background at its edges, same as a resting card — leaving the
  // backdrop up instead would flatten that contrast away, same bug as leaving it on permanently.
  const backdropOpaque = phase === "growing" || phase === "shrinking";
  // Hiding it (entering "open") is animated, crossfading with the composer's own fade-in so there's no
  // gap between the two. Appearing (entering "growing" or "shrinking") is instant — at that instant
  // there's nothing else visible yet to crossfade WITH: the outgoing layer already finished fading
  // during the previous phase, so a slow fade-in here would itself be the gap.
  const backdropOpacityMs = phase === "open" ? FADE_MS : 0;
  // Old content's own fade-out is quick (a quarter of the grow) — only on the way OUT (entering
  // "growing"). Fading back in on the way IN (entering "shrinking") keeps the full GROW_MS, unchanged.
  const closedOpacityMs = phase === "growing" ? CLOSED_FADE_MS : GROW_MS;

  return (
    <li
      className={`relative self-stretch${composerMounted ? " row-span-2" : ""}`}
      style={{
        maxHeight: grown ? OPEN_CAP : CLOSED_CAP,
        overflow: "hidden",
        transition: `max-height ${GROW_MS}ms var(--ease-swim)`,
      }}
    >
      {/* Always mounted (never conditionally rendered) so background-color always has a real previous
          frame to transition FROM — a freshly-mounted div has no "before" value, so a conditional mount
          here would just snap straight to its target colour instead of crossfading (see backdropOpaque
          above for why it's only ever actually VISIBLE during growing/shrinking). closedTint under the
          tile/card, crossfading to OPEN_TINT under the composer, in step with the size itself so the box
          is never mid-grow over blank space. */}
      <div
        aria-hidden
        className="absolute inset-0"
        style={{
          opacity: backdropOpaque ? 1 : 0,
          background: grown ? OPEN_TINT : closedTint,
          borderRadius: COMPOSER_RADIUS,
          WebkitMaskImage: BACKDROP_MASK,
          maskImage: BACKDROP_MASK,
          WebkitMaskComposite: "source-in",
          maskComposite: "intersect",
          pointerEvents: "none",
          transition: `background-color ${GROW_MS}ms var(--ease-swim), opacity ${backdropOpacityMs}ms var(--ease-swim)`,
        }}
      />
      {/* h-full always: `closed` (NewRecipeTile, RecipeCard) fills it with their own h-full, which needs
          a DEFINITE height to resolve against — without this, at rest (no "absolute inset-0" yet) this
          div would shrink-to-fit its child instead, and the child's own h-full would resolve to nothing,
          collapsing it to its bare minimum instead of matching the row's other cards.
          Overlaid (out of flow) once the composer exists, so it never contributes to the shell's own
          height — only the in-flow `open` content below should ever drive that. */}
      <div
        className={`h-full${composerMounted ? " absolute inset-0" : ""}`}
        style={{
          opacity: closedVisible ? 1 : 0,
          pointerEvents: closedVisible ? "auto" : "none",
          transition: `opacity ${closedOpacityMs}ms var(--ease-swim)`,
        }}
      >
        {closed}
      </div>
      {composerMounted && (
        // NOT absolutely positioned: this is what the shell's height actually grows to fit (up to
        // maxHeight's cap) — an absolute box has no natural height of its own to grow toward, which is
        // exactly why the shell used to stop short of the composer's real content.
        <div
          style={{
            opacity: composerVisible ? 1 : 0,
            pointerEvents: composerVisible ? "auto" : "none",
            transition: `opacity ${FADE_MS}ms var(--ease-swim)`,
          }}
        >
          {open}
        </div>
      )}
    </li>
  );
}
