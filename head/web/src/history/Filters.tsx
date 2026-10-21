import type { Outcome } from "../api";
import { Select } from "../app/Select";
import { Chip } from "../app/Chip";

/**
 * The archive's filter panel — sidebar, sticky, mirroring the Queue composer's slot. No borders, no
 * hairline selects: facets are quiet pressable chips (a recess that lights when active), the same
 * form-from-light language as the rest of the interface.
 *
 * The search box is the one thing the facets can't express — free text over script / #id / who — so a
 * specific person is found by typing, while the facets stay coarse (Mine, an outcome, an arm).
 */

export type Who = "everyone" | "me";

export type Filter = {
  query: string;
  who: Who;
  // Empty means "all outcomes" — the default. Adding one narrows to just the chosen outcomes.
  outcomes: Set<Outcome>;
  arm: string; // "" = any arm
};

export const EMPTY_FILTER: Filter = { query: "", who: "everyone", outcomes: new Set(), arm: "" };

// Two rows of two: the quiet outcomes (done, cancelled) on top, the loud ones (failed, retracted) below.
// `settling` is deliberately NOT offered: it is a ≤30s transient (api.ts's outcomeOf), so a facet for it
// would be a filter whose results emptied while you looked at them.
const OUTCOMES: Outcome[] = ["done", "cancelled", "failed", "retracted"];

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <span className="label mb-2 block">{label}</span>
      {children}
    </div>
  );
}

export function Filters({
  value,
  onChange,
  arms,
}: {
  value: Filter;
  onChange: (f: Filter) => void;
  /** Distinct arms present in the archive — the only ones worth offering. */
  arms: string[];
}) {
  const set = (patch: Partial<Filter>) => onChange({ ...value, ...patch });

  const toggleOutcome = (o: Outcome) => {
    const next = new Set(value.outcomes);
    next.has(o) ? next.delete(o) : next.add(o);
    set({ outcomes: next });
  };

  return (
    <aside className="surface [--sink:35%] space-y-6" style={{ padding: "calc(var(--pad) + 50px)" }}>
      <input
        className="field"
        placeholder="script, user, or id"
        value={value.query}
        onChange={(e) => set({ query: e.target.value })}
        aria-label="Search the archive"
      />

      <Group label="User">
        <div className="flex gap-1">
          <Chip on={value.who === "everyone"} onClick={() => set({ who: "everyone" })}>
            Everyone
          </Chip>
          <Chip on={value.who === "me"} onClick={() => set({ who: "me" })}>
            Me
          </Chip>
        </div>
      </Group>

      <Group label="Outcome">
        {/* Two content-width columns → done / cancelled on row one, failed / retracted on row two, packed
            together (grid-cols-2 would stretch each column to 50% and leave a big gap between the pair). */}
        <div className="grid grid-cols-[max-content_max-content] gap-1">
          {OUTCOMES.map((o) => (
            <Chip key={o} on={value.outcomes.has(o)} onClick={() => toggleOutcome(o)}>
              {o}
            </Chip>
          ))}
        </div>
      </Group>

      <Group label="Arm">
        <Select
          ariaLabel="Filter by arm"
          value={value.arm}
          onChange={(arm) => set({ arm })}
          options={[{ value: "", label: "All arms" }, ...arms.map((a) => ({ value: a, label: a }))]}
        />
      </Group>
    </aside>
  );
}
