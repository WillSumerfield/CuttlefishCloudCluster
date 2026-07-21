import type { Fold } from "./fold";
import { levelOf, LEVEL_LABEL, tideOklch } from "./fold";

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
    <section className="surface [--sink:40%] px-12 py-8 pb-10 md:px-22 md:py-22 md:pb-12 w-full md:w-1/5 h-64">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
        <span
          className="num mx-auto"
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
        <span className="label pl-3.5" style={{ color: tint, letterSpacing: "0.28em", fontSize: 14}}>
          {LEVEL_LABEL[level]}
        </span>
      </div>
    </section>
  );
}
