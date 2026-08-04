import type { Arm } from "../api";
import { ArmRow } from "./ArmRow";
import { Tip } from "../app/Tip";
import { ROSTER_COLS } from "./format";

/**
 * The whole fleet, one flat table in a single surface — every Arm, everyone's. The label head names the
 * columns once; each row lines up beneath it by grid position, no rules between. This is the page's
 * centre of gravity, so it takes the full page width (the reason the nav is a left rail, not a top bar).
 */
export function Roster({ arms, isAdmin }: { arms: Arm[]; isAdmin: boolean }) {
  return (
    <section>
      <Tip className="label mb-3 pl-1 inline-block" text="A machine somebody has donated to the cluster. Its owner earns Karma whenever someone else's work runs on it.">All arms</Tip>
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
            <Tip text="Who donated this machine. They earn the Karma its work is billed for — the Admin names the owner when they authorize the machine, so a donor never declares it themselves.">Owner</Tip>
            <span>State</span>
            <span className="text-right">GPUs</span>
            <Tip className="text-right" text="The card's memory. A Spawn is eligible for every card holding at least what it asked for.">VRAM</Tip>
            <Tip className="text-right" text="What a card can actually do, measured by running a benchmark on it when its Arm joined — not read off a spec sheet. It's what stops a slower machine earning more for being slow.">Power</Tip>
            <Tip text="The machine's address on the cluster's own private network. Nothing outside the cluster can reach it there.">Mesh</Tip>
          </div>

          <ul>
            {arms.map((a) => (
              <ArmRow key={a.name} arm={a} />
            ))}
          </ul>
        </>
      )}
      </div>
    </section>
  );
}
