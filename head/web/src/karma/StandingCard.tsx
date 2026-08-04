import { Link } from "../app/router";
import { Tip } from "../app/Tip";
import type { Standing } from "../api";

function formatSigned(n: number) {
  const s = n >= 0 ? "+" : "−";
  return `${s}${Math.abs(n).toFixed(0)}`;
}

/**
 * Your own two numbers: what you hold, and what your live Spawns are holding of it.
 *
 * The Escrow list is the whole reason the total is worth showing — "−140" is a fact you can do nothing
 * with, and "the sweep on arm-mantle-01 is holding 90 of it" is one you can. Each claim links to its
 * Spawn, since cancelling it is the action the number implies.
 */
export function StandingCard({ standing }: { standing: Standing }) {
  const { balance, escrow, escrow_spawns: escrowSpawns } = standing;
  const n = escrowSpawns.length;

  return (
    <section className="surface [--sink:40%] px-12 py-8 pb-10 md:px-20 md:py-10 md:pb-20">
      <div className="grid grid-cols-1 md:grid-cols-[1.15fr_1fr] gap-10">
        {/* Balance — sits low in its half, and the one number on the page big enough to be the point */}
        <div className="flex h-full items-end justify-center pb-6">
          <div className="flex items-baseline gap-4">
            <span
              className="num"
              style={{
                fontSize: 88,
                lineHeight: 1,
                fontWeight: 300,
                letterSpacing: "-0.03em",
                color: "var(--gold-hi)",
                textShadow: "0 0 40px color-mix(in oklab, var(--gold) 30%, transparent)",
              }}
            >
              {formatSigned(balance)}
            </span>
            <Tip className="label" text="Your standing in the cluster — earned by donating hardware, spent by running work. It only ever decides who goes first when two people want the cluster at once; there is no balance at which your work is refused, and on an idle cluster it does nothing at all.">Karma</Tip>
          </div>
        </div>

        {/* Escrow */}
        {/* No rule between the columns. Space separates them; a 1px line is the most machine-drawn
            mark there is, and this interface has none. */}
        <div className="relative md:pl-10">
          <div className="mt-3 flex items-baseline gap-3">
            <span
              className="num"
              style={{
                fontSize: 36,
                fontWeight: 400,
                color: "var(--bronze)",
              }}
            >
              −{escrow.toFixed(0)}
            </span>
            <Tip className="text-sm text-ink-mute" text="Karma held against work you have outstanding. It isn't a charge — it's released when the Spawn ends and the real cost is taken instead. While it's held it lowers your standing, so flooding the queue costs you your own place in it.">
              ESCROW, {n} Spawn{n === 1 ? "" : "s"}
            </Tip>

          </div>

          <ul className="mt-2.5 space-y-2.5">
            {escrowSpawns.map((s) => (
              <Link
                key={s.job_id}
                to={`/spawn/${s.job_id}`}
                className="relative flex items-center justify-between rounded-[10px] px-3 py-2.5"
              >
                {/*
                 * The fill, on its own layer BEHIND the content, dissolved to nothing at every edge by
                 * an ALPHA mask — the same idiom the surfaces use, just at a row's scale (the card's own
                 * 50px fade would swallow a 40px row whole). Crossed linear alpha ramps, intersected, so
                 * all four sides AND the corners fade. Behind the content so the dot and the number stay
                 * fully crisp while the plate melts. No border, no hairline, no hard edge anywhere.
                 */}
                <span
                  aria-hidden
                  className="absolute inset-0 rounded-[10px]"
                  style={{
                    background:
                      "linear-gradient(180deg, color-mix(in oklab, var(--shoal) 42%, transparent) 0%, color-mix(in oklab, var(--body) 46%, transparent) 100%)",
                    WebkitMaskImage:
                      "linear-gradient(to right, transparent 0, rgba(0,0,0,0.5) 8px, black 16px, black calc(100% - 16px), rgba(0,0,0,0.5) calc(100% - 8px), transparent 100%), linear-gradient(to bottom, transparent 0, rgba(0,0,0,0.5) 5px, black 10px, black calc(100% - 10px), rgba(0,0,0,0.5) calc(100% - 5px), transparent 100%)",
                    WebkitMaskComposite: "source-in",
                    maskImage:
                      "linear-gradient(to right, transparent 0, rgba(0,0,0,0.5) 8px, black 16px, black calc(100% - 16px), rgba(0,0,0,0.5) calc(100% - 8px), transparent 100%), linear-gradient(to bottom, transparent 0, rgba(0,0,0,0.5) 5px, black 10px, black calc(100% - 10px), rgba(0,0,0,0.5) calc(100% - 5px), transparent 100%)",
                    maskComposite: "intersect",
                  }}
                />
                <div className="relative flex min-w-0 items-center gap-3">
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{
                      background: "var(--bronze)",
                      boxShadow: "0 0 12px 2px color-mix(in oklab, var(--bronze) 45%, transparent)",
                      animation: "breathe 3.2s ease-in-out infinite",
                    }}
                  />
                  <span className="num truncate text-ink" style={{ fontSize: 13, letterSpacing: "0.02em" }}>
                    {s.job_id}
                  </span>
                  <span className="truncate text-[12px] text-ink-mute">
                    {[s.script, s.node].filter(Boolean).join(" · ")}
                  </span>
                </div>
                <span className="num relative text-ink-soft" style={{ fontSize: 13 }}>
                  −{s.amount.toFixed(0)}
                </span>
              </Link>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
