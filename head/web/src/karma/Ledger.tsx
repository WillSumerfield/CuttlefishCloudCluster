import { useMemo } from "react";
import { Link } from "../app/router";
import { edgeFade } from "../app/edgeFade";
import { Tip } from "../app/Tip";
import type { Ledger as LedgerData, LedgerRow } from "../api";

/**
 * Your economic history: the events that happened, and the Balance they imply.
 *
 * The curve is where Decay lives — the only honest place for it. Decay is `exp(-Δt/τ)` applied
 * continuously BETWEEN rows, not a transaction; a "drifted toward zero" row would imply a stored Balance
 * to drift, and there is none (Balance is summed on read). Curve draws the sag, rows sit on it as marks.
 *
 * No Arm column: the ledger records who paid whom for which Dive, not which card it ran on — that
 * belongs to the Dive, which each row links to.
 */

const VB_W = 640, VB_H = 96, PAD = 8;

function amountColor(amount: number) {
  return amount >= 0 ? "var(--gold-hi)" : "var(--bronze)";
}

/** An Override has no counterparty by construction — Karma with no source was conjured, with no
 *  destination destroyed — so it is named for the act, not the party. */
function overrideLabel(r: LedgerRow) {
  return r.amount >= 0 ? "granted" : "taken";
}

/** Absolute clock time, relative day — the two things you actually want from a ledger timestamp. A
 *  settlement at 14:22 means nothing without knowing it was today. */
function when(at: string): string {
  const t = new Date(at);
  const clock = t.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  const days = Math.floor((Date.now() - t.getTime()) / 86_400_000);
  if (days <= 0) return `${clock} · today`;
  if (days === 1) return `${clock} · yesterday`;
  if (days < 14) return `${clock} · ${days}d ago`;
  return `${clock} · ${t.toLocaleDateString(undefined, { day: "numeric", month: "short" })}`;
}

/** The Balance over the window, with each event marked where it landed on it. Zero is drawn because the
 *  whole shape is "distance from zero, decaying back toward it" — without that line the sag is just a
 *  wobble. Credit above it, debt below, in the same two colours the amounts use. */
export function Curve({ data }: { data: LedgerData }) {
  const { path, zeroY, marks } = useMemo(() => {
    const pts = data.curve;
    const vals = pts.map((p) => p.balance);
    // The scale is symmetric about zero so a creditor's curve and a debtor's are the same shape
    // mirrored, rather than two differently-stretched pictures of the same economy.
    const mag = Math.max(1, ...vals.map(Math.abs));
    const x = (i: number) => (i / Math.max(1, pts.length - 1)) * VB_W;
    const y = (v: number) => PAD + (VB_H - PAD * 2) * (1 - (v / mag + 1) / 2);

    const t0 = pts.length ? Date.parse(pts[0].at) : 0;
    const t1 = pts.length ? Date.parse(pts[pts.length - 1].at) : 1;
    const span = Math.max(1, t1 - t0);

    return {
      path: pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.balance).toFixed(1)}`).join(""),
      zeroY: y(0),
      // An event's mark sits at its own timestamp on the curve, not at its row's index: the rows are
      // evenly spaced in the list and the events are not evenly spaced in time.
      marks: data.rows
        .map((r) => ({ r, f: (Date.parse(r.at) - t0) / span }))
        .filter((m) => m.f >= 0 && m.f <= 1)
        .map((m) => {
          const i = Math.round(m.f * (pts.length - 1));
          return { id: m.r.id, x: x(i), y: y(pts[i]?.balance ?? 0), amount: m.r.amount };
        }),
    };
  }, [data]);

  return (
    <svg
      viewBox={`0 0 ${VB_W} ${VB_H}`}
      preserveAspectRatio="none"
      className="block h-28 w-full"
      style={edgeFade()}
    >
      <line x1={0} x2={VB_W} y1={zeroY} y2={zeroY} stroke="var(--ink-mute)" strokeWidth={0.5} opacity={0.35} />
      <path d={path} fill="none" stroke="var(--gold)" strokeWidth={1.5} vectorEffect="non-scaling-stroke" opacity={0.85} />
      {marks.map((m) => (
        <circle key={m.id} cx={m.x} cy={m.y} r={2.5} fill={amountColor(m.amount)} />
      ))}
    </svg>
  );
}

export function Ledger({ data }: { data: LedgerData }) {
  const { rows } = data;

  return (
    <div className="min-w-0">
      <header className="mb-4 px-2">
        {/* Quiet, like the page title: muted ink at 300 weight, so the rows are what the eye lands on. */}
        <Tip text="Every time Karma actually moved: a Dive settling, or an Admin granting or taking some.">
          <h2 style={{ fontSize: 20, fontWeight: 300, letterSpacing: "-0.02em" }} className="text-ink-mute">
            Ledger
          </h2>
        </Tip>
      </header>

      {/* The curve gets the column's full width — it is a shape over time, and width is what shows one. */}
      <div className="mb-8 px-2">
        <Curve data={data} />
        <Tip
          className="mt-2 block text-[12px] text-ink-mute"
          text="Your Balance over the last 30 days. Between events it drifts back toward zero on its own, so debt is forgiven and hoarded standing evaporates — that sag is the Decay. It never appears as a row because it isn't something that happens, it's what happens in between."
        >
          Last 30 days. Between events your Balance drifts back toward zero.
        </Tip>
      </div>

      {rows.length === 0 ? (
        <p className="px-2 py-6 text-[13px] text-ink-mute">
          Nothing settled yet. A Dive pays its Arm's owner when it ends, and that is the first row you'll see.
        </p>
      ) : (
        // Rows stay a compact block: four short fields spread over a wide panel leave a gulf mid-row.
        <ul className="max-w-[720px]">
          {rows.map((row, idx) => {
            /*
             * Rows sink as they age. `depth` runs 0 (newest, right at the surface) → 1 (oldest, down in
             * the dark): the row dims, so the ledger recedes into the abyss rather than ending at a
             * boundary. No per-row background — a per-row pool bands into stripes over the moving abyss.
             */
            const depth = rows.length > 1 ? idx / (rows.length - 1) : 0;

            return (
              <li
                key={row.id}
                className="grid grid-cols-[56px_1fr_auto_auto] items-baseline gap-x-6 px-2 py-2.5"
                style={{ opacity: 1 - depth * 0.42 }}
              >
                <span className="num" style={{ fontSize: 16, fontWeight: 500, color: amountColor(row.amount) }}>
                  {row.amount >= 0 ? "+" : "−"}
                  {Math.abs(row.amount).toFixed(0)}
                </span>

                <span className="min-w-0 truncate text-[14px]">
                  {row.kind === "override" ? (
                    <>
                      <Tip
                        className="text-ink"
                        text="An Admin adding or removing Karma directly. It's the only movement with nobody on the other side of it."
                      >
                        {overrideLabel(row)}
                      </Tip>
                      {row.note && <span className="text-ink-mute"> · “{row.note}”</span>}
                    </>
                  ) : row.counterparty === null ? (
                    // No counterparty is not a missing name — it is the point: a Dive on a commons Arm
                    // destroys what it pays.
                    <Tip
                      className="text-ink-mute"
                      text="This ran on a machine the Admin owns, so there was nobody to pay — the Karma was destroyed rather than transferred."
                    >
                      to the commons
                    </Tip>
                  ) : (
                    <Tip
                      text="Karma moves from the person who ran the work to the owner of the machine it ran on. Running on your own Arm is free."
                    >
                      <span className="text-ink-mute">{row.amount >= 0 ? "from " : "to "}</span>
                      <span className="text-ink">{row.counterparty}</span>
                    </Tip>
                  )}
                </span>

                {row.job_id !== null ? (
                  <Link to={`/dive/${row.job_id}`} className="num text-[13px] text-ink-soft hover:text-ink">
                    Dive {row.job_id}
                  </Link>
                ) : (
                  <span />
                )}

                <span className="num text-right text-[12px] text-ink-mute" style={{ minWidth: 110 }}>
                  {when(row.at)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
