import { yourStanding, escrowSpawns } from "./mock";

function formatSigned(n: number) {
  const s = n >= 0 ? "+" : "−";
  return `${s}${Math.abs(n).toFixed(0)}`;
}

export function StandingCard() {
  const { balance, escrow, escrowSpawns: n } = yourStanding;

  return (
    <section className="surface p-12 md:p-20">
      <div className="grid grid-cols-1 md:grid-cols-[1.15fr_1fr] gap-10">
        {/* Balance */}
        <div>
          <div className="mt-3 flex items-baseline gap-4">
            <span
              className="data"
              style={{
                fontSize: 64,
                lineHeight: 1,
                fontWeight: 400,
                letterSpacing: "-0.02em",
                color: "var(--gold-hi)",
                textShadow:
                  "0 0 40px color-mix(in oklab, var(--gold) 30%, transparent)",
              }}
            >
              {formatSigned(balance)}
            </span>
            <span className="text-sm font-bold uppercase text-ink-mute">KARMA</span>
          </div>
        </div>

        {/* Escrow */}
        <div className="relative md:pl-10">
          <div
            aria-hidden
            className="pointer-events-none absolute inset-y-2 left-0 hidden w-px md:block"
            style={{
              background:
                "linear-gradient(180deg, transparent 0%, color-mix(in oklab, var(--ink) 15%, transparent) 45%, transparent 100%)",
            }}
          />
          <div className="label">Escrow</div>
          <div className="mt-3 flex items-baseline gap-3">
            <span
              className="data"
              style={{
                fontSize: 36,
                fontWeight: 400,
                letterSpacing: "-0.01em",
                color: "var(--bronze)",
              }}
            >
              −{escrow.toFixed(0)}
            </span>
            <span className="text-sm text-ink-mute">
              {n} Spawn{n === 1 ? "" : "s"}
            </span>

          </div>

          <ul className="mt-5 space-y-2.5">
            {escrowSpawns.map((s) => (
              <li
                key={s.id}
                className="flex items-center justify-between rounded-[10px] px-3 py-2.5"
                style={{
                  background:
                    "linear-gradient(180deg, color-mix(in oklab, var(--shoal) 55%, transparent) 0%, color-mix(in oklab, var(--body) 60%, transparent) 100%)",
                  boxShadow:
                    "inset 0 1px 0 0 color-mix(in oklab, var(--ink) 6%, transparent)",
                }}
              >
                <div className="flex min-w-0 items-center gap-3">
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{
                      background: "var(--bronze)",
                      boxShadow: "0 0 12px 2px color-mix(in oklab, var(--bronze) 45%, transparent)",
                      animation: "breathe 3.2s ease-in-out infinite",
                    }}
                  />
                  <span className="data truncate text-ink" style={{ fontSize: 12 }}>
                    {s.id}
                  </span>
                  <span className="truncate text-[12px] text-ink-mute">{s.note}</span>
                </div>
                <span className="data text-ink-soft" style={{ fontSize: 12 }}>
                  −{s.amount.toFixed(0)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
