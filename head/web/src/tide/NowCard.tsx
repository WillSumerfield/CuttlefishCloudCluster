import type { Fold } from "./fold";
import { levelOf, LEVEL_LABEL, tideOklch } from "./fold";
import { Tip } from "../app/Tip";

/**
 * The now-reading — the Tide page's StandingCard. One number: what a Spawn costs THIS hour against a
 * typical hour, as a multiplier (cost ∝ Demand, so the ratio is the price story without ever being a
 * price). Coloured by the tide it names — teal at Low, violet at High. It states the shape; it never
 * warns, gates, or asks you to confirm. Karma ranks, it never gates.
 */
export function NowCard({ fold }: { fold: Fold }) {
  const { multiplier } = fold;
  const level = levelOf(multiplier);
  const tint = tideOklch(multiplier);

  return (
    <section className="px-12 py-8 pb-10 md:px-22 md:py-22 md:pb-12 w-full md:w-1/5 h-64">
      {/* No plate under this one: the reading floats on the water it's describing, so the card is the
          number rather than a box holding it. Centred on both axes because there's no plate edge left to
          align against — `h-full` gives the row the section's height to centre within. */}
      <div className="flex h-full flex-wrap items-baseline justify-center gap-x-6 gap-y-2">
        {/* The multiplier IS a Demand ratio, and the level word IS the Tide — so the two terms land on
            the two marks that actually mean them, rather than both on the card. */}
        {/* py-3 -my-3: `lineHeight: 0.7` on an 88px glyph leaves a line box shorter than the ink, so the
            top and bottom of the digits would sit outside the hover target. The padding grows the target
            to the glyph; the negative margin hands the space back, and baseline alignment keeps the number
            where it was. Tight to the ink deliberately — the trigger covers the horizon's own hover. */}
        <Tip
          className="mx-auto py-3 -my-3"
          text="An estimate of the karma cost multiplier based on previous demand. The multiplier is the ratio of the current hour's demand to a typical hour's demand. Real spawns may cost more or less than this estimate, and the multiplier is not a quote or a guarantee."
        >
          <span
            className="num"
            style={{
              fontSize: 88,
              lineHeight: 0.7,
              fontWeight: 300,
              letterSpacing: "-0.03em",
              color: tint,
              textShadow: `0 0 40px color-mix(in oklab, ${tint} 34%, transparent)`,
            }}
          >
            {multiplier.toFixed(1)}×
          </span>
        </Tip>
        <Tip
          className="label"
          text="What an hour on the cluster has typically cost, by hour of day. It's a shape to plan around, not a quote — nothing here will warn you or ask you to confirm before you submit."
        >
          {/* Tracking puts a full letter-space after the last letter and none before the first, so a
              centred box centres the word plus its trailing gap — the word itself lands left of true
              centre. The matching padding on the near side is the counterweight. */}
          <span style={{ color: tint, letterSpacing: "0.28em", fontSize: 14, paddingLeft: "0.28em" }}>
            {LEVEL_LABEL[level]}
          </span>
        </Tip>
      </div>
    </section>
  );
}
