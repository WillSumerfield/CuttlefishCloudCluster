/**
 * The way onto the shelf — the first grid cell, card-shaped so it reads as an empty slot waiting to be
 * filled. Identical in construction to NewRecipeTile (same recess, same crossed fade, no rim); it says a
 * different word, and that is the whole of the difference.
 */
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

export function NewWorkspaceTile({ onClick }: { onClick: () => void }) {
  return (
    <div className="min-h-[150px] h-full">
      <button
        onClick={onClick}
        className="group relative flex h-full w-full flex-col items-center justify-center gap-2"
        style={{ minHeight: "inherit" }}
      >
        <span
          aria-hidden
          className="absolute inset-0"
          style={{
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
          New workspace
        </span>
      </button>
    </div>
  );
}
