import { useState } from "react";
import type { Arm } from "../api";
import { Tip } from "../app/Tip";
import { CloudDrift } from "../queue/CloudDrift";
import { IridBloom } from "../queue/IridBloom";
import { armState, toneColor, vram, power, type ArmAction } from "./format";

/**
 * The machines you own — a band above the roster, shown only if you own one. Cards, not roster rows,
 * because these are few and they carry a control the roster's rows don't: the panic switch. Here the full
 * motion vocabulary is legible: the passing cloud when a card is working, the chromatic bloom while it
 * benchmarks or waits for its donor, still when it's idle or retracted.
 *
 * Retract drains the Arm and stops its work immediately (a fixed 3-minute grace, then force-kill). It is
 * destructive, so it confirms inline. Return gives a retracted Arm back — no Admin, no new token, because
 * a retracted Arm never left the cluster.
 */
function MyArmCard({ arm, onAct }: { arm: Arm; onAct: (name: string, action: ArmAction) => void }) {
  const { word, tone } = armState(arm);
  const cloud = word === "allocated";
  const bloom = word === "benchmarking" || word.startsWith("awaiting");
  const retracted = word === "retracted";
  const [confirm, setConfirm] = useState(false);

  return (
    <li className="shard relative" style={{ padding: "2.4rem 2.4rem" }}>
      {cloud ? <CloudDrift /> : bloom ? <IridBloom /> : null}

      <div className="relative z-10">
        {/* A retracted Arm recedes — its facts dim and grey. The Return control below stays at full
            strength, since reviving it is the whole reason to look at this card. */}
        <div style={retracted ? { opacity: 0.5, filter: "saturate(0.5)" } : undefined}>
        <div className="flex items-baseline justify-between gap-2">
          <span className="num truncate text-[15px] text-ink">{arm.name}</span>
          {arm.gpu && <span className="shrink-0 text-[12px] text-ink-mute">{arm.gpu}</span>}
        </div>

        <div className="mt-1.5 flex items-center gap-2">
          <span
            aria-hidden
            className="h-2 w-2 shrink-0 rounded-full"
            style={{
              background: toneColor[tone],
              boxShadow: tone === "live" ? "0 0 9px 1px color-mix(in oklab, var(--irid-dim) 60%, transparent)" : "none",
              opacity: tone === "wait" ? 0.5 : 1,
            }}
          />
          <span className="text-[13px]" style={{ color: tone === "alarm" ? "var(--alarm)" : "var(--ink-soft)" }}>
            {word}
          </span>
        </div>

        <div className="mt-1 flex flex-wrap items-center gap-x-3 text-[12px] text-ink-mute">
          <span className="num">{arm.gpus} GPU{arm.gpus === 1 ? "" : "s"}</span>
          <span aria-hidden>·</span>
          <span className="num">{vram(arm.vram_mb)}</span>
          <span aria-hidden>·</span>
          <span className="num">{power(arm.power)}</span>
        </div>
        </div>

        <div className="mt-4">
          {retracted ? (
            <Tip text="Give a retracted machine back. It never actually left the cluster, so this needs no Admin and no new token.">
              <button className="btn-ghost" onClick={() => onAct(arm.name, "return")}>
                Return
              </button>
            </Tip>
          ) : confirm ? (
            <div className="flex items-center gap-2">
              <span className="text-[12px] text-ink-mute">Stops work now, 3-min grace.</span>
              <button className="btn-ghost" onClick={() => setConfirm(false)}>
                Keep
              </button>
              <button
                className="btn-ghost"
                style={{ color: "var(--alarm)" }}
                onClick={() => {
                  setConfirm(false);
                  onAct(arm.name, "retract");
                }}
              >
                Retract
              </button>
            </div>
          ) : (
            <Tip text="Take your machine back right now. Work on it gets a short grace period and then dies — and it settles at zero, so whoever was running pays nothing for a run you ended.">
              <button className="btn-ghost" onClick={() => setConfirm(true)}>
                Retract
              </button>
            </Tip>
          )}
        </div>
      </div>
    </li>
  );
}

export function MyArms({ arms, onAct }: { arms: Arm[]; onAct: (name: string, action: ArmAction) => void }) {
  if (arms.length === 0) return null;
  return (
    <section>
      <h2 className="label mb-3 pl-1">My arms</h2>
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(260px,360px))] gap-4">
        {arms.map((a) => (
          <MyArmCard key={a.name} arm={a} onAct={onAct} />
        ))}
      </ul>
    </section>
  );
}
