import type { ArchivedSpawn, Outcome } from "../api";
import { Tip } from "../app/Tip";
import { Link } from "../app/router";
import { duration, placedLabel } from "../queue/format";

/**
 * One finished Spawn in the archive. NOT a shard: the archive is a single surface and rows are soft pools
 * of light inside it (the Karma-ledger treatment), dimming with `depth` so the list recedes downward.
 * There is NO motion layer — nothing here is running or waiting, and "finished things do not move" is what
 * lets the queue's cloud/bloom mean "live" at all. A History row is still by design.
 *
 * Outcome colour is binary, per temp/DESIGN.md §State colour: done and cancelled are quiet (ink-mute);
 * failed and retracted are the loud ones and turn --alarm. No status pills, no glyphs — just the word.
 *
 * The whole row is a link to the Spawn's detail (verdict, logs); there are no per-row buttons.
 */

const LOUD: Record<Outcome, boolean> = {
  done: false,
  cancelled: false,
  failed: true,
  retracted: true,
  // Emphatically quiet. `settling` is the ≤30s before the tick stamps an outcome (api.ts's outcomeOf),
  // and colouring a Spawn that has not been judged yet is exactly the bug it was added to fix.
  settling: false,
};

// What each ending actually means for what you were charged — the part the word alone never says.
const WHY: Record<Outcome, string> = {
  done: "The run reached the end of your script and exited cleanly.",
  cancelled:
    "Somebody ended this run early. It still settles for the seconds it actually held — choosing to stop doesn't make it free.",
  failed:
    "The run ended without a clean exit — a crash, or the machine going away underneath it. It's settled for what it used and is never silently retried.",
  retracted:
    "The machine's owner took it back mid-run. This is the one ending that costs the submitter nothing.",
  settling:
    "This run has just ended and the cluster is still working out what it did and what it cost. Its outcome appears within about half a minute.",
};

function Verdict({ outcome, exit }: { outcome: Outcome; exit: number | null }) {
  const loud = LOUD[outcome];
  return (
    <Tip text={WHY[outcome]} className={loud ? "text-[color:var(--alarm)]" : undefined}>
      {outcome}
      {/* Exit code rides along only where one exists — failed carries it; cancelled/retracted never ran
          to an exit. done is exit 0 and says so quietly. */}
      {exit !== null && (
        <>
          {" "}
          <span className="num">exit {exit}</span>
        </>
      )}
    </Tip>
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
              <span className="num">{placedLabel(spawn.node)}</span>
              {spawn.held !== null && (
                <>
                  <span aria-hidden>·</span>
                  <span className="num">{duration(spawn.held)} held</span>
                </>
              )}
            </div>
          </div>

          {/* Settled Karma — right-aligned and tabular so costs compare down the column, and GOLD,
              because Karma is gold everywhere else in this interface and a cost is still Karma. The
              "pigment is standing, not cost" rule doesn't apply: pigment is the umber→gold RAMP that
              encodes a balance, and a flat gold isn't on it, so nothing here reads as standing.
              Named, because a bare number in a column of prose is a quantity of nothing. Rounded: the
              settled amount is a float, and 1.3339583333333335 is not a price anyone reads. */}
          <span className="num shrink-0 text-[13px]" style={{ minWidth: "3ch", textAlign: "right", color: "var(--gold)" }}>
            {spawn.cost === null ? (
              "—"
            ) : (
              <>
                {Math.round(spawn.cost)} <span className="label !text-[10px]">karma</span>
              </>
            )}
          </span>
        </div>
      </Link>
    </li>
  );
}
