import type { ArchivedSpawn, Outcome } from "../api";
import { Link } from "../app/router";
import { duration, armLabel } from "../queue/format";

/**
 * One finished Spawn in the archive. NOT a shard: the archive is a single surface and rows are soft pools
 * of light inside it (the Karma-ledger treatment), dimming with `depth` so the list recedes downward.
 * There is NO motion layer — nothing here is running or waiting, and "finished things do not move" is what
 * lets the queue's cloud/bloom mean "live" at all. A History row is still by design.
 *
 * Outcome colour is binary, per temp/DESIGN.md §State colour: done and cancelled are quiet (ink-mute);
 * failed and retracted are the loud ones and turn --alarm. No status pills, no glyphs — just the word.
 *
 * The whole row is a link to the Spawn's detail (verdict, logs, outputs); there are no per-row buttons.
 */

const LOUD: Record<Outcome, boolean> = { done: false, cancelled: false, failed: true, retracted: true };

function Verdict({ outcome, exit }: { outcome: Outcome; exit: number | null }) {
  const loud = LOUD[outcome];
  return (
    <span style={loud ? { color: "var(--alarm)" } : undefined}>
      {outcome}
      {/* Exit code rides along only where one exists — failed carries it; cancelled/retracted never ran
          to an exit. done is exit 0 and says so quietly. */}
      {exit !== null && (
        <>
          {" "}
          <span className="num">exit {exit}</span>
        </>
      )}
    </span>
  );
}

export function HistoryRow({ spawn, depth }: { spawn: ArchivedSpawn; depth: number }) {
  // Same falloff as the Karma ledger: the pool of light under a row weakens and the row dims as it ages,
  // so the archive sinks into the dark instead of stopping on a line. Newest (depth 0) sits at the surface.
  const band = Math.max(0, 20 * (1 - depth * 0.9));
  const dim = 1 - depth * 0.42;
  return (
    <li>
      <Link
        to={`/spawn/${spawn.id}`}
        className="history-row px-2 py-3.5"
        style={{ "--band": `${band.toFixed(1)}%`, "--dim": dim } as React.CSSProperties}
      >
        <div className="flex items-center gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-2">
              <span className="num truncate text-[15px] text-ink">{spawn.name || `spawn ${spawn.id}`}</span>
              <span className="num shrink-0 text-[12px] text-ink-mute">#{spawn.id}</span>
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px] text-ink-mute">
              {spawn.submitted_by && <span className="text-ink-soft">{spawn.submitted_by}</span>}
              <span aria-hidden>·</span>
              <Verdict outcome={spawn.outcome} exit={spawn.exit} />
              <span aria-hidden>·</span>
              <span className="num">{armLabel(spawn.node)}</span>
              {spawn.held !== null && (
                <>
                  <span aria-hidden>·</span>
                  <span className="num">{duration(spawn.held)} held</span>
                </>
              )}
            </div>
          </div>

          {/* Settled Karma — quiet, right-aligned, tabular so costs compare down the column. Plain ink,
              never pigment (pigment is standing, not cost). A dash before it was priced. */}
          <span className="num shrink-0 text-[13px] text-ink-mute" style={{ minWidth: "3ch", textAlign: "right" }}>
            {spawn.cost === null ? "—" : spawn.cost}
          </span>
        </div>
      </Link>
    </li>
  );
}
