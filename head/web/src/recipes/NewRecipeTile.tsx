/**
 * The way onto the shelf — the first cell of the grid, card-shaped so it reads as an empty slot waiting to
 * be filled. No border (the law): it's a quiet recess, lower luminance than the cards around it, that lifts
 * toward gold on hover. Clicking it hands the page over to the composer.
 *
 * Its rim dissolves the same way every surface does — the crossed smoothstep ramps of `.surface`'s fade,
 * inlined here because the tile is a recess (a dark fill), not the raised ::before plate that `.surface`
 * draws, so it can't just borrow the class without looking like a card.
 */
// px depth per axis; the visible ramp is 2× this. Tuned generous now that the tile stretches to match
// the taller Recipe cards beside it (self-stretch, RecipesPage.tsx) rather than sitting at its own
// 150px minimum — the old, shallower depth left most of a ~220px+ box at full opaque fill, reading as a
// solid dark block instead of a dissolving recess.
const FADE = 40;
const ramp = (dir: string) =>
  `linear-gradient(${dir},` +
  ` transparent 0,` +
  ` rgba(0,0,0,.156) ${FADE * 0.5}px,` +
  ` rgba(0,0,0,.5) ${FADE}px,` +
  ` rgba(0,0,0,.844) ${FADE * 1.5}px,` +
  ` #000 ${FADE * 2}px,` +
  ` #000 calc(100% - ${FADE * 2}px),` +
  ` rgba(0,0,0,.844) calc(100% - ${FADE * 1.5}px),` +
  ` rgba(0,0,0,.5) calc(100% - ${FADE}px),` +
  ` rgba(0,0,0,.156) calc(100% - ${FADE * 0.5}px),` +
  ` transparent 100%)`;
const FADE_MASK = `${ramp("to right")}, ${ramp("to bottom")}`;

export function NewRecipeTile({ onClick }: { onClick: () => void }) {
  return (
    // min-h/h-full: ComposerSlot (its parent, RecipesPage.tsx) owns the actual grid item and its
    // self-stretch now — this just fills whatever box that hands it.
    <div className="min-h-[150px] h-full">
      <button
        onClick={onClick}
        className="group relative flex h-full w-full flex-col items-center justify-center gap-2"
        style={{ minHeight: "inherit" }}
      >
        {/* The recess fill, dissolving at every edge via the crossed fade — no hard rim, like everything else. */}
        <span
          aria-hidden
          className="absolute inset-0"
          style={{
            // `--abyss`, not literal black — it's the same blue-ish hue family as every other tone token
            // in the app (--well/--body/--shoal), just its darkest point, so this reads as a deep recess
            // rather than a true-black hole punched through the page.
            background: "color-mix(in oklab, var(--abyss) 75%, var(--shoal))",
            WebkitMaskImage: FADE_MASK,
            maskImage: FADE_MASK,
            WebkitMaskComposite: "source-in",
            maskComposite: "intersect",
          }}
        />
        <span
          aria-hidden
          className="relative text-[24px] leading-none text-ink-mute transition-colors group-hover:text-[color:var(--gold)]"
        >
          +
        </span>
        <span className="relative text-[13px] text-ink-mute transition-colors group-hover:text-ink-soft">
          New recipe
        </span>
      </button>
    </div>
  );
}
