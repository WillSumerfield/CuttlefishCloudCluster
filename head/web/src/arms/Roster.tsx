import type { Arm } from "../api";
import { ArmRow } from "./ArmRow";
import { ROSTER_COLS, type ArmAction } from "./format";

/**
 * The whole fleet, one flat table in a single surface — every Arm, everyone's. The label head names the
 * columns once; each row lines up beneath it by grid position, no rules between. This is the page's
 * centre of gravity, so it takes the full page width (the reason the nav is a left rail, not a top bar).
 */
export function Roster({
  arms,
  isAdmin,
  onAct,
}: {
  arms: Arm[];
  isAdmin: boolean;
  onAct: (name: string, action: ArmAction) => void;
}) {
  return (
    <section>
      <h2 className="label mb-3 pl-1">All arms</h2>
      <div
        className="surface"
        style={{ padding: "calc(var(--pad) + 14px)", "--fade-x": "30px", "--fade-y": "30px" } as React.CSSProperties}
      >
      {arms.length === 0 ? (
        <p className="px-3.5 py-6 text-[14px] text-ink-mute">
          No arms enrolled yet.{isAdmin ? " Authorize one to hand a donor the command that joins their box." : " Machines appear here as they're donated."}
        </p>
      ) : (
        <>
          {/* Label head — named once, uppercase and muted, the numeric columns right-aligned to match. */}
          <div className="label grid items-center gap-3 px-5 pb-4" style={{ gridTemplateColumns: ROSTER_COLS }}>
            <span>Name</span>
            <span>Owner</span>
            <span>State</span>
            <span className="text-right">GPUs</span>
            <span className="text-right">VRAM</span>
            <span className="text-right">Power</span>
            <span>Mesh</span>
          </div>

          <ul>
            {arms.map((a) => (
              <ArmRow key={a.name} arm={a} isAdmin={isAdmin} onAct={onAct} />
            ))}
          </ul>
        </>
      )}
      </div>
    </section>
  );
}
