import { ledger, type LedgerRow } from "./mock";

function amountColor(kind: LedgerRow["kind"]) {
  if (kind === "earn")  return "var(--gold-hi)";
  if (kind === "spend") return "var(--bronze)";
  return "var(--ink-mute)"; // decay
}
function amountPrefix(kind: LedgerRow["kind"]) {
  if (kind === "earn")  return "+";
  if (kind === "spend") return "−";
  return "~";
}
function kindLabel(kind: LedgerRow["kind"]) {
  if (kind === "earn")  return "earned";
  if (kind === "spend") return "paid";
  return "decay";
}

export function Ledger() {
  return (
    <section className="surface p-12 md:p-20">
      <header className="mb-6">
        {/* Quiet, like the page title: muted ink at 300 weight, so the rows are what the eye lands on. */}
        <h2 style={{ fontSize: 20, fontWeight: 300, letterSpacing: "-0.02em" }} className="text-ink-mute">
          Ledger
        </h2>
      </header>


      <div className="grid grid-cols-[110px_1fr_170px] items-center gap-x-6 px-2 pb-3">
        <span className="label">Karma</span>
        <span className="label">Transaction</span>
        <span className="label text-right">When</span>
      </div>

      <ul>
        {ledger.map((row, idx) => {
          /*
           * Rows sink as they age. `depth` runs 0 (newest, right at the surface) → 1 (oldest, down
           * in the dark): the band of light under a row weakens and the whole row dims, so the
           * ledger recedes into the abyss rather than ending at a boundary.
           */
          const depth = ledger.length > 1 ? idx / (ledger.length - 1) : 0;

          // The band is a soft pool of light with no edge anywhere — NOT a zebra stripe. A stripe's
          // gradient starts abruptly at the row's top edge, which draws a hard line across the card.
          // Clamped at 0: a negative percentage makes the whole color-mix invalid and the browser
          // discards the background entirely, so rows past the fade point would lose their band.
          const band = Math.max(0, 20 * (1 - depth * 0.9));

          return (
          <li
            key={row.id}
            className="grid grid-cols-[110px_1fr_170px] items-start gap-x-6 rounded-[10px] px-2 py-4"
            style={{
              background: `radial-gradient(130% 130% at 50% 45%, color-mix(in oklab, var(--shoal) ${band.toFixed(1)}%, transparent) 0%, transparent 72%)`,
              opacity: 1 - depth * 0.42,
            }}
          >
            <div className="flex items-baseline gap-1">
              <span
                className="num"
                style={{
                  fontSize: 17,
                  fontWeight: 500,
                  color: amountColor(row.kind),
                }}
              >
                {amountPrefix(row.kind)}
                {row.amount.toFixed(0)}
              </span>
            </div>

            <div className="min-w-0">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="text-[15px] text-ink">
                  {row.kind === "decay" ? (
                    <>Karma drifted toward zero</>
                  ) : row.kind === "earn" ? (
                    <>
                      <span className="text-ink-mute">from </span>
                      <span className="text-ink">{row.counterparty}</span>
                    </>
                  ) : (
                    <>
                      <span className="text-ink-mute">to </span>
                      <span className="text-ink">{row.counterparty}</span>
                    </>
                  )}
                </span>
                <span className="label">{kindLabel(row.kind)}</span>
              </div>
              {(row.spawnId || row.arm) && (
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
                  {row.spawnId && (
                    <span className="num text-ink-soft" style={{ letterSpacing: "0.02em" }}>{row.spawnId}</span>
                  )}
                  {row.arm && (
                    <>
                      {row.spawnId && <span className="text-ink-mute">·</span>}
                      <span className="text-ink-mute">{row.arm}</span>
                    </>
                  )}
                </div>
              )}

            </div>

            <div className="num text-right text-ink-mute" style={{ fontSize: 12 }}>
              {row.when}
            </div>
          </li>
          );
        })}
      </ul>
    </section>
  );
}
