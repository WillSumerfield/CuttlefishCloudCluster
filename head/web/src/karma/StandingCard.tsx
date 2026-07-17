import { yourStanding, escrowSpawns } from "./mock";

function formatSigned(n: number) {
  const s = n >= 0 ? "+" : "−";
  return `${s}${Math.abs(n).toFixed(0)}`;
}

export function StandingCard() {
  const { balance, escrow, escrowSpawns: n } = yourStanding;

  return (
    <section className="surface [--sink:40%] p-12 pb-20 md:p-20 md:pb-32">
      <div className="grid grid-cols-1 md:grid-cols-[1.15fr_1fr] gap-10">
        {/* Balance — centred in its half, and the one number on the page big enough to be the point */}
        <div className="flex h-full items-center justify-center">
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
            <span className="label">Karma</span>
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
            <span className="text-sm text-ink-mute">
            ESCROW, {n} Spawn{n === 1 ? "" : "s"}
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
                  <span className="num truncate text-ink" style={{ fontSize: 13, letterSpacing: "0.02em" }}>
                    {s.id}
                  </span>
                  <span className="truncate text-[12px] text-ink-mute">{s.note}</span>
                </div>
                <span className="num text-ink-soft" style={{ fontSize: 13 }}>
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
