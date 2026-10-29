import { Link } from "../app/router";
import { Tip } from "../app/Tip";
import type { Standing } from "../api";

function formatSigned(n: number) {
  const s = n >= 0 ? "+" : "−";
  return `${s}${Math.abs(n).toFixed(0)}`;
}

/**
 * Your own numbers: what you hold, and what your live Dives are holding of it.
 * The left column of the Karma panel — no plate of its own, it sits on the panel it shares with the Ledger.
 *
 * The Escrow list is the whole reason the total is worth showing — "−140" is a fact you can do nothing
 * with, and "the sweep on arm-mantle-01 is holding 90 of it" is one you can. Each claim links to its
 * Dive, since cancelling it is the action the number implies.
 */
export function StandingCard({ standing }: { standing: Standing }) {
  const { balance, escrow, escrow_dives: escrowDives } = standing;
  const n = escrowDives.length;

  return (
    <div>
      {/* Balance — the one number on the page big enough to be the point */}
      <div className="flex items-baseline gap-3">
        <span
          className="num"
          style={{
            fontSize: 72,
            lineHeight: 1,
            fontWeight: 300,
            letterSpacing: "-0.03em",
            color: "var(--gold-hi)",
            textShadow: "0 0 40px color-mix(in oklab, var(--gold) 30%, transparent)",
          }}
        >
          {formatSigned(balance)}
        </span>
        <Tip className="text-[14px] text-ink-mute" text="Your standing in the cluster — earned by donating hardware, spent by running work. It only ever decides who goes first when two people want the cluster at once; there is no balance at which your work is refused, and on an idle cluster it does nothing at all.">Karma</Tip>
      </div>

      {/* Escrow */}
      <div className="mt-8">
        {n === 0 ? (
          <Tip className="text-[13px] text-ink-mute" text="Karma held against work you have outstanding. It isn't a charge — it's released when the Dive ends and the real cost is taken instead.">
            Nothing held right now
          </Tip>
        ) : (
          <div className="flex items-baseline gap-2.5">
            <span className="num" style={{ fontSize: 24, fontWeight: 400, color: "var(--bronze)" }}>
              −{escrow.toFixed(0)}
            </span>
            <Tip className="text-[13px] text-ink-mute" text="Karma held against work you have outstanding. It isn't a charge — it's released when the Dive ends and the real cost is taken instead. While it's held it lowers your standing, so flooding the queue costs you your own place in it.">
              held by {n} Dive{n === 1 ? "" : "s"}
            </Tip>
          </div>
        )}

          <ul className="mt-2.5 space-y-2.5">
            {escrowDives.map((s) => (
              <Link
                key={s.job_id}
                to={`/dive/${s.job_id}`}
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
                    {[s.main && `~/${s.main}`, s.node].filter(Boolean).join(" · ")}
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
  );
}
