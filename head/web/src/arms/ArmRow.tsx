import type { Arm } from "../api";
import { CloudDrift, CLOUD_PRESETS } from "../queue/CloudDrift";
import { Tip } from "../app/Tip";
import { armState, toneColor, vram, power, mesh, ROSTER_COLS } from "./format";

/**
 * One machine in the roster. NOT a card — a flush row inside the roster's single surface, its cells
 * lining up under the label head by grid position alone (no rules; there are none). The only motion is
 * the passing cloud on an ALLOCATED row — a machine that is working. Everything else is still, because a
 * roster is a table you scan, and twenty moving rows would fight that.
 *
 * Owner is plain ink, never pigment: pigment means Karma.
 *
 * There are no Admin controls on this row, and nothing is missing behind that. Admitting, benchmarking
 * and removing an Arm are Head-CLI-only, permanently — the Head API exposes no admin-write endpoints at
 * all — so the row offers no button rather than one that always fails.
 */
export function ArmRow({ arm }: { arm: Arm }) {
  const { word, tone, working } = armState(arm);
  const retracted = word === "retracted";

  return (
    <li
      className="arm-row group relative overflow-hidden"
      // Retracted Arms recede — dimmed and greyed, a machine withdrawn from the cluster but not gone. (The
      // hover highlight and the cloud carry their own row-sized feather in the `arm-row` utility.)
      style={{ opacity: retracted ? 0.45 : undefined, filter: retracted ? "saturate(0.5)" : undefined } as React.CSSProperties}
    >
      {working && (
        // A roster row is wide and short — the queue's half-width, -10° band would be a ~450px slab
        // clipped to a 58px strip, reading as a steep slash. The `roster` preset gives it a narrow,
        // near-upright streak that drifts the long way across, slow and spaced out (see CloudDrift).
        <CloudDrift config={CLOUD_PRESETS.roster} />
      )}

      <div
        className="relative z-10 grid items-center gap-3 px-5"
        style={{ gridTemplateColumns: ROSTER_COLS, paddingTop: 13, paddingBottom: 13 }}
      >
        {/* NAME — with the card model, and any declared Arm Features, as quiet subtitles (useful, not
            columns of their own — most Arms have none). */}
        <div className="min-w-0">
          <div className="num truncate text-[14px] text-ink">{arm.name}</div>
          {arm.gpu && <div className="truncate text-[11px] text-ink-mute">{arm.gpu}</div>}
          {arm.features.length > 0 && (
            <div className="truncate text-[11px] text-ink-mute">
              {arm.features.map((f) => f.name).join(", ")}
            </div>
          )}
        </div>

        {/* OWNER — plain ink. Unowned (awaiting a donor) reads as a dash, never blank. */}
        <div className="truncate text-[13px] text-ink-soft">{arm.owner ?? "—"}</div>

        {/* STATE — a dot in its tone, then the word. Colour only on live (irid) and down (alarm). */}
        <div className="flex items-center gap-2">
          <span
            aria-hidden
            className="h-2 w-2 shrink-0 rounded-full"
            style={{
              background: toneColor[tone],
              boxShadow:
                tone === "live"
                  ? "0 0 9px 1px color-mix(in oklab, var(--irid-dim) 60%, transparent)"
                  : tone === "alarm"
                    ? "0 0 9px 1px color-mix(in oklab, var(--alarm) 45%, transparent)"
                    : "none",
              opacity: tone === "wait" ? 0.5 : 1,
            }}
          />
          {retracted ? (
            <Tip className="truncate text-[13px] text-ink-soft" text="The owner has paused this machine. It's still enrolled and still on the mesh; it just isn't accepting work.">{word}</Tip>
          ) : (
            <span className="truncate text-[13px]" style={{ color: tone === "alarm" ? "var(--alarm)" : "var(--ink-soft)" }}>
              {word}
            </span>
          )}
        </div>

        {/* GPUS · VRAM · POWER — the machine's measure. Right-aligned, tabular, so a column reads as a column. */}
        <div className="num text-right text-[13px] text-ink-soft">{arm.gpus || "—"}</div>
        <div className="num text-right text-[13px] text-ink-soft">{vram(arm.vram_mb)}</div>
        <div className="num text-right text-[13px] text-ink-soft">{power(arm.power)}</div>

        {/* MESH — where it lives on the Mesh, or a dash before it joins. */}
        <div className="num truncate text-[13px] text-ink-mute">{mesh(arm.mesh_ip)}</div>
      </div>

    </li>
  );
}
