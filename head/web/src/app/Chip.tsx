/**
 * A quiet toggle chip — lit = raised, ink; unlit = flush, muted. Colour is never spent on a control.
 * Shared by the archive filters and the composer's toggles; all want "pick one of a few" rather than
 * a checkbox.
 */
export function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className="rounded-[var(--radius-control)] px-3 py-1.5 text-[13px] transition-[background,color] duration-150"
      style={{
        background: on ? "color-mix(in oklab, var(--shoal) 45%, transparent)" : "transparent",
        color: on ? "var(--ink)" : "var(--ink-mute)",
        fontWeight: on ? 500 : 400,
      }}
    >
      {children}
    </button>
  );
}
