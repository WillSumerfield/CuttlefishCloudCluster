import type { CSSProperties } from "react";

/**
 * The waterline dissolve — soften an element's two side edges and its foot so a drawing bleeds into the
 * page instead of ending on a hard rectangle. Works on a <canvas> (the Tide horizon) and an <svg> (the
 * usage bands). Top stays solid.
 *
 * One element, two gradient layers multiplied with `mask-composite: intersect` — a pixel's alpha is
 * xFade × yFade, so the corners fall off fastest. Standard `mask-*` only, no `-webkit-` twins: on Blink
 * the prefixed props ALIAS the standard longhands, so a stray `-webkit-mask-composite` silently reset
 * `intersect` to the default `add` and left the sides opaque (the hard edge). Modern Chrome/Safari/FF
 * all take the unprefixed form.
 *
 * @param side   fraction of the width each left/right edge fades over (0.1 = 10%)
 * @param bottom fraction of the height the foot fades over
 * @param clearR fraction of the width at the right kept fully clear (the right fade ends before it) — room
 *               for something to float there over the abyss, like the Tide page's legend
 */
export function edgeFade(side = 0.2, bottom = 0.2, clearR = 0): CSSProperties {
  const x = `linear-gradient(90deg, transparent, #000 ${side * 100}%, #000 ${(1 - side - clearR) * 100}%, transparent ${(1 - clearR) * 100}%)`;
  const y = `linear-gradient(180deg, #000 0%, #000 ${(1 - bottom) * 100}%, transparent 100%)`;
  return {
    maskImage: `${x}, ${y}`,
    maskRepeat: "no-repeat",
    maskSize: "100% 100%",
    maskComposite: "intersect",
  };
}
