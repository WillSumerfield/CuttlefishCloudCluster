import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * A hover explanation.
 *
 * Nothing marks a target at rest — no glyph, underline, or cursor change. Coverage is wide enough that
 * pointing at something and getting an explanation is expected, not a discovery; a marker on forty labels
 * would be forty pieces of furniture on a spare interface. Cost: text a User must read to avoid a mistake
 * belongs on the page, not here.
 *
 * Panel is portalled and fixed-positioned: half the targets sit inside a `.surface` (fade mask) or the
 * Dive modal, and an in-flow absolute panel gets clipped by both. Positioned from the trigger's rect
 * on open, flipped to the other side when it would leave the viewport.
 *
 * Leaving the trigger always closes it, even if the pointer lands on the panel itself — a tip is a quick
 * gloss on the control it's attached to, not a second thing to go read. Keyboard focus opens it too;
 * Escape/blur close it. Touch gets nothing — a stated gap, not an oversight: this is a LAN tool for
 * submitting GPU work, nobody does that from a phone.
 */

/** The knobs worth tuning on a tooltip. Edit, save, watch. */
const TIP = {
  /** Inner padding, px — the gap between the gold edge and the first word. */
  padX: 20,
  padY: 16,

  /** Widest the panel may get, px. Text wraps inside it; longer copy grows downward, not sideways. */
  maxWidth: 340,

  /** Gap between the trigger and the panel, px. */
  gap: 10,

  /** The gold edge — the ONE bordered thing in the Dashboard (everything else dissolves at its rim by an
   *  alpha mask), because a tooltip floats over content it has nothing to do with and a melt would blend
   *  it into whatever it's covering. Any CSS colour; `--gold` is full strength, `--gold-hi` brighter,
   *  `--bronze` dimmer. */
  borderColor: "color-mix(in oklab, var(--gold) 60%, transparent)",
  borderWidth: 1,

  /** Corner radius, px. */
  radius: 12,

  /**
   * A gold bloom outside the border, px — 0 for none. Small values read as the edge catching light;
   * past ~30 it stops being an edge and becomes a halo the panel sits inside.
   */
  glow: 14,

  /**
   * How far the plate sinks toward `--deep` at its bottom edge (`.surface`'s `--sink`, default 100%).
   * Low, because a shallow panel reaches its bottom long before a steep ramp has finished — the same
   * reason a short card keeps this low. Raise for a moodier, heavier panel.
   */
  sink: "25%",

  /**
   * Plate colour — three of them, since a surface is a vertical gradient not a fill: `shoal` at the top
   * edge, `body` through the middle, sinking toward `deep` at the bottom (how far set by `sink`).
   *
   * `null` = the page's global tokens (index.css:45-49); setting any value here recolours THIS PANEL ONLY.
   * To warm toward economy gold, try shoal: "oklch(0.34 0.045 90)", body: "oklch(0.29 0.038 88)" — keep
   * lightness (1st number) roughly as-is, move hue (3rd), or the panel stops matching depth.
   */
  tint: {
    // The page's own plate, one step down in lightness (0.34 → 0.28, 0.29 → 0.23) — hue and chroma
    // untouched, so it reads as the same material sitting deeper rather than a different colour. A
    // panel that floats over a card wants to be darker than what it covers, or it competes with it.
    shoal: "oklch(0.28 0.042 195)" as string | null,
    body: "oklch(0.23 0.036 200)" as string | null,
    // Left alone: `sink` is 25%, so the bottom edge only travels a quarter of the way here anyway.
    deep: null as string | null,
  },

  /** How long the pointer must rest before it opens, ms. The whole point on an unmarked target: a
   *  pointer sweeping across a form must not leave a wake of panels behind it. */
  openDelay: 150,

  /** Long enough to cross the gap from trigger to panel without a flicker, short enough that a pointer
   *  merely passing over a label does not leave a panel hanging. Keyboard closes instantly instead. */
  closeDelay: 120,
};

// The three plate colours, resolved once: a tint override if TIP.tint names one, else the page's own
// token. Written out here rather than inline so the gradient below reads as a gradient.
const SHOAL = TIP.tint.shoal ?? "var(--shoal)";
const BODY = TIP.tint.body ?? "var(--body)";
const DEEP = TIP.tint.deep ?? "var(--deep)";

type Pos = { top: number; left: number; place: "above" | "below" };

export function Tip({
  text,
  children,
  className,
  focusable = true,
}: {
  /** One to three sentences of prose. Not a node — every tip is the same shape, and a tip that wants
   *  markup is a tip that has grown into documentation and belongs on the page instead. */
  text: string;
  children: ReactNode;
  className?: string;
  /** False when the child is already focusable (input/select/button) — otherwise the wrapper takes its
   *  own tab stop before the control's, so Tab would need pressing twice. Focus still opens the panel
   *  either way: React's onFocus is focusin, firing as the child receives focus. */
  focusable?: boolean;
}) {
  const id = useId();
  const ref = useRef<HTMLSpanElement | null>(null);
  const openTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  // A click on the trigger (e.g. opening a Select's own menu) focuses the clicked control, and that
  // focus bubbles up here as onFocus — which would reopen the panel right after the pointerdown below
  // just closed it. Set for the one focus event a pointerdown causes, so only THAT focus is swallowed;
  // a later Tab into the same control still opens it normally.
  const suppressFocus = useRef(false);
  const [pos, setPos] = useState<Pos | null>(null);

  const clear = () => {
    if (openTimer.current) window.clearTimeout(openTimer.current);
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    openTimer.current = closeTimer.current = null;
  };

  const place = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const w = Math.min(TIP.maxWidth, window.innerWidth - 16);
    // Centred on the trigger, then pulled back inside the viewport rather than allowed to overhang —
    // a panel half off the screen is worse than one that isn't quite centred.
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
    // Below by default; above when there isn't room, which is what happens to every tip in the lower
    // third of a long page. Estimated from the text length — measuring would need a second paint.
    const estimated = TIP.padY * 2 + Math.ceil(text.length / 44) * 18;
    const below = window.innerHeight - r.bottom;
    const useAbove = below < estimated + TIP.gap && r.top > below;
    setPos({
      left,
      top: useAbove ? r.top - TIP.gap : r.bottom + TIP.gap,
      place: useAbove ? "above" : "below",
    });
  }, [text.length]);

  const open = useCallback(
    (delay: number) => {
      clear();
      openTimer.current = window.setTimeout(place, delay);
    },
    [place],
  );

  const close = useCallback((delay: number) => {
    clear();
    closeTimer.current = window.setTimeout(() => setPos(null), delay);
  }, []);

  // Scrolling or resizing while open moves the trigger out from under the panel. Re-place rather than
  // close: closing on scroll would fight a User who is scrolling to read the thing being explained.
  useEffect(() => {
    if (!pos) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setPos(null);
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
      window.removeEventListener("keydown", onKey);
    };
  }, [pos, place]);

  useEffect(() => clear, []);

  return (
    <>
      <span
        ref={ref}
        className={className}
        tabIndex={focusable ? 0 : undefined}
        aria-describedby={pos ? id : undefined}
        // A trigger IS an event target by definition, so it claims its own hits no matter what it sits
        // inside: `pointer-events` inherits, and a Tip under an inert overlay (TidePage's z-10 wrapper,
        // which lets the pointer fall through to the water) would otherwise go quietly dead — a hover
        // explanation that never explains, with nothing on screen to say so. Inline rather than a class:
        // `className` is the caller's, and two competing Tailwind utilities resolve by stylesheet order.
        style={{ pointerEvents: "auto" }}
        // Pointer, not mouse: a pen hovers, and `pointerenter` on a touch tap would open a panel the
        // User cannot dismiss by moving away. `pointerType` is what tells them apart.
        onPointerEnter={(e) => e.pointerType !== "touch" && open(TIP.openDelay)}
        onPointerLeave={(e) => e.pointerType !== "touch" && close(TIP.closeDelay)}
        // A click is the User acting on the target, not reading about it — e.g. opening a Select's own
        // menu while still hovering the trigger, which pointerleave never fires for. Closes instantly,
        // no delay: the thing they clicked is about to draw its own panel in roughly the same place.
        onPointerDown={() => {
          clear();
          setPos(null);
          suppressFocus.current = true;
        }}
        // Keyboard opens with no delay — a delay after a deliberate Tab is just latency, where after an
        // incidental pointer sweep it is the whole point.
        onFocus={() => {
          if (suppressFocus.current) {
            suppressFocus.current = false;
            return;
          }
          open(0);
        }}
        onBlur={() => close(0)}
      >
        {children}
      </span>

      {pos &&
        createPortal(
          <div
            id={id}
            role="tooltip"
            // NOT `.surface`: a surface dissolves at its rim by an alpha mask, this panel has a hard gold
            // edge instead, deliberately — the one element floating over content it has nothing to do
            // with needs a stated boundary, not a melt. Plate gradient reproduces `.surface`'s
            // (index.css:189-208) since this panel doesn't wear that utility.
            style={{
              position: "fixed",
              left: pos.left,
              top: pos.top,
              transform: pos.place === "above" ? "translateY(-100%)" : undefined,
              zIndex: 200,
              // The tint knobs, resolved here rather than via custom properties — nothing downstream
              // reads them now that the ::before plate is gone.
              background: `linear-gradient(180deg, color-mix(in oklab, ${SHOAL} 35%, ${BODY}) 0%, ${BODY} 25%, color-mix(in oklab, ${DEEP} ${TIP.sink}, ${BODY}) 100%)`,
              border: `${TIP.borderWidth}px solid ${TIP.borderColor}`,
              borderRadius: TIP.radius,
              maxWidth: TIP.maxWidth,
              padding: `${TIP.padY}px ${TIP.padX}px`,
              fontSize: 12.5,
              lineHeight: 1.55,
              color: "var(--ink-soft)",
              // Depth is luminance here, not a drop shadow — but a floating panel still needs to sit
              // off the page, so this is the one soft shadow, kept well under the plate's own lighting.
              boxShadow: `0 18px 30px rgba(0,0,0,0.45)${TIP.glow ? `, 0 0 ${TIP.glow}px color-mix(in oklab, ${TIP.borderColor} 45%, transparent)` : ""}`,
              // Fades in where it lands rather than sliding: a panel that travels reads as a thing
              // arriving, and this is meant to be the page answering, not an object appearing.
              animation: "tip-in 120ms ease-out",
            } as React.CSSProperties}
          >
            {text}
          </div>,
          document.body,
        )}
    </>
  );
}
