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
        <h2 style={{ fontSize: 20, fontWeight: 500, letterSpacing: "-0.01em" }} className="text-ink">
          Your ledger
        </h2>
      </header>


      <div className="grid grid-cols-[110px_1fr_170px] items-center gap-x-6 px-2 pb-3">
        <span className="label font-bold">KARMA</span>
        <span className="label">Movement</span>
        <span className="label text-right">When</span>
      </div>

      <ul>
        {ledger.map((row, idx) => (
          <li
            key={row.id}
            className="grid grid-cols-[110px_1fr_170px] items-start gap-x-6 rounded-[10px] px-2 py-4"
            style={{
              background:
                idx % 2 === 0
                  ? "linear-gradient(180deg, color-mix(in oklab, var(--shoal) 22%, transparent) 0%, transparent 100%)"
                  : "transparent",
            }}
          >
            <div className="flex items-baseline gap-1">
              <span
                className="data"
                style={{
                  fontSize: 15,
                  fontWeight: 500,
                  color: amountColor(row.kind),
                  letterSpacing: "-0.01em",
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
                    <span className="data text-ink-soft">{row.spawnId}</span>
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

            <div className="data text-right text-ink-mute" style={{ fontSize: 12 }}>
              {row.when}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
