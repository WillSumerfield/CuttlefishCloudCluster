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
 * No Arm column: the ledger records who paid whom for which Spawn, not which card it ran on — that
 * belongs to the Spawn, which each row links to.
 */

const VB_W = 640, VB_H = 96, PAD = 8;

function amountColor(amount: number) {
  return amount >= 0 ? "var(--gold-hi)" : "var(--bronze)";
}

/** What the row DID, from your side. An Override has no counterparty by construction — Karma with no
 *  source was conjured, with no destination destroyed — so it is named for the act, not the party. */
function kindLabel(r: LedgerRow) {
  if (r.kind === "override") return r.amount >= 0 ? "granted" : "taken";
  return r.amount >= 0 ? "earned" : "paid";
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
function Curve({ data }: { data: LedgerData }) {
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
      className="block h-24 w-full"
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
    <section className="surface p-12 md:p-20">
      <header className="mb-6">
        {/* Quiet, like the page title: muted ink at 300 weight, so the rows are what the eye lands on. */}
        <Tip text="Every time Karma actually moved: a Dive settling, or an Admin granting or taking some.">
          <h2 style={{ fontSize: 20, fontWeight: 300, letterSpacing: "-0.02em" }} className="text-ink-mute">
            Ledger
          </h2>
        </Tip>
      </header>

      <div className="mb-10">
        <Curve data={data} />
        <div className="mt-2 flex items-baseline justify-between">
          <Tip
            className="text-[12px] text-ink-mute"
            text="Your Balance over the last 30 days. Between events it drifts back toward zero on its own, so debt is forgiven and hoarded standing evaporates — that sag is the drift, and it never appears as a row because it isn't something that happens, it's what happens in between."
          >
            Between events your Balance drifts back toward zero — the sag is the Decay.
          </Tip>
          <span className="label shrink-0">Last 30 days</span>
        </div>
      </div>

      {rows.length === 0 ? (
        <p className="px-2 py-6 text-[13px] text-ink-mute">
          Nothing settled yet. A Dive pays its Arm's owner when it ends, and that is the first row you'll see.
        </p>
      ) : (
        <>
          <div className="grid grid-cols-[110px_1fr_170px] items-center gap-x-6 px-2 pb-3">
            <Tip className="label" text="Your standing in the cluster — earned by donating hardware, spent by running work. It only ever decides who goes first when two people want the cluster at once; there is no balance at which your work is refused, and on an idle cluster it does nothing at all.">Karma</Tip>
            <span className="label">Transaction</span>
            <span className="label text-right">When</span>
          </div>

          <ul>
            {rows.map((row, idx) => {
              /*
               * Rows sink as they age. `depth` runs 0 (newest, right at the surface) → 1 (oldest, down
               * in the dark): the band of light under a row weakens and the whole row dims, so the
               * ledger recedes into the abyss rather than ending at a boundary.
               */
              const depth = rows.length > 1 ? idx / (rows.length - 1) : 0;

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
                    <span className="num" style={{ fontSize: 17, fontWeight: 500, color: amountColor(row.amount) }}>
                      {row.amount >= 0 ? "+" : "−"}
                      {Math.abs(row.amount).toFixed(0)}
                    </span>
                  </div>

                  <div className="min-w-0">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                      <span className="text-[15px] text-ink">
                        {row.counterparty === null ? (
                          // No counterparty is not a missing name — it is the point. An Override
                          // conjures Karma from nowhere; a Spawn on a commons Arm destroys what it pays.
                          <Tip
                            className="text-ink-mute"
                            text={
                              row.kind === "override"
                                ? "An Admin adding or removing Karma directly. It's the only movement with nobody on the other side of it."
                                : "This ran on a machine the Admin owns, so there was nobody to pay — the Karma was destroyed rather than transferred."
                            }
                          >
                            {row.kind === "override" ? "by an Admin" : "to the commons"}
                          </Tip>
                        ) : (
                          <>
                            <span className="text-ink-mute">{row.amount >= 0 ? "from " : "to "}</span>
                            <span className="text-ink">{row.counterparty}</span>
                          </>
                        )}
                      </span>
                      <Tip
                        className="label"
                        text={
                          row.kind === "override"
                            ? "An Admin adding or removing Karma directly. It's the only movement with nobody on the other side of it."
                            : "Karma moves from the person who ran the work to the owner of the machine it ran on. Running on your own Arm is free."
                        }
                      >
                        {kindLabel(row)}
                      </Tip>
                    </div>
                    {(row.job_id !== null || row.note) && (
                      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
                        {row.job_id !== null && (
                          <Link
                            to={`/spawn/${row.job_id}`}
                            className="num text-ink-soft"
                            style={{ letterSpacing: "0.02em" }}
                          >
                            {row.job_id}
                          </Link>
                        )}
                        {row.job_id !== null && row.note && <span className="text-ink-mute">·</span>}
                        {row.note && <span className="text-ink-mute">{row.note}</span>}
                      </div>
                    )}
                  </div>

                  <div className="num text-right text-ink-mute" style={{ fontSize: 12 }}>
                    {when(row.at)}
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
