import type { ArchivedDive } from "../api";
import { HistoryRow } from "./HistoryRow";

/**
 * The archive: one flat, newest-first stream (outcome is a filter, not a structural split — an archive is
 * chronological by nature). Two distinct empties, because the difference is real: an archive with nothing
 * in it yet vs. a filter that matched nothing.
 */
export function ArchiveList({
  dives,
  total,
}: {
  dives: ArchivedDive[];
  /** Rows before filtering — tells "nothing has finished" apart from "nothing matches". */
  total: number;
}) {
  return (
    <section className="surface" style={{ padding: "calc(var(--pad) + 50px)" }}>
      {/* The one column that needs naming: a bare number is ambiguous. Everything else names itself. */}
      <div className="mb-2 flex items-center gap-4 px-2">
        <span className="flex-1" />
      </div>

      {dives.length === 0 ? (
        <p className="px-2 pt-2 text-[13px] text-ink-mute">
          {total === 0 ? "No dives have finished yet." : "No dives match these filters."}
        </p>
      ) : (
        <ul>
          {dives.map((s, i) => (
            <HistoryRow key={s.id} dive={s} depth={dives.length > 1 ? i / (dives.length - 1) : 0} />
          ))}
        </ul>
      )}
    </section>
  );
}
