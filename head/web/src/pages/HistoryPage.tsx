import { useEffect, useMemo, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { session } from "../app/session";
import { getHistory, Unauthorized, type ArchivedSpawn } from "../api";
import { ArchiveList } from "../history/ArchiveList";
import { Filters, EMPTY_FILTER, type Filter } from "../history/Filters";

/**
 * The searchable archive of finished work. One flat, newest-first stream, filtered from a sidebar — the
 * inverse of the Queue: finished Spawns, no motion, no colour but the loud outcomes. Unlike the Queue it
 * does NOT poll — the archive is a record, not a live board, so it's fetched once. New completions appear
 * on the next visit.
 */
export function HistoryPage() {
  const { logout } = useAuth();
  const [rows, setRows] = useState<ArchivedSpawn[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [filter, setFilter] = useState<Filter>(EMPTY_FILTER);

  useEffect(() => {
    getHistory()
      .then(setRows)
      .catch((e) => {
        // A session can expire while the archive is open; fall back to sign-in rather than a dead page.
        if (e instanceof Unauthorized) logout();
        // Any other error (Head unreachable, a 5xx) leaves rows empty → the empty state.
      })
      .finally(() => setLoaded(true));
  }, [logout]);

  // The arms worth offering are the ones that actually appear in the archive.
  const arms = useMemo(
    () => [...new Set(rows.map((r) => r.node).filter((n): n is string => !!n))].sort(),
    [rows],
  );

  const shown = useMemo(() => {
    const q = filter.query.trim().toLowerCase();
    return rows
      .filter((r) => {
        if (filter.who === "me" && r.submitted_by !== session.username) return false;
        if (filter.outcomes.size > 0 && !filter.outcomes.has(r.outcome)) return false;
        if (filter.arm && r.node !== filter.arm) return false;
        if (q) {
          const hay = `${r.name} #${r.id} ${r.submitted_by ?? ""}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      })
      .sort((a, b) => b.finished_at - a.finished_at);
  }, [rows, filter]);

  return (
    <Page
      title="History"
      actions={
        loaded && rows.length > 0 ? (
          <span className="text-[13px] text-ink-mute">
            <span className="num text-ink-soft">{shown.length}</span>
            {shown.length !== rows.length && (
              <>
                {" "}
                of <span className="num text-ink-soft">{rows.length}</span>
              </>
            )}{" "}
            spawn{shown.length === 1 ? "" : "s"}
          </span>
        ) : null
      }
    >
      {/* Filters left, archive right — the same two-column shape as the Queue (composer left, list right),
          so the format reads as one family. Below lg they stack, filters first; filters stick on scroll. */}
      <div className="grid gap-8 lg:grid-cols-[minmax(300px,380px)_1fr] lg:items-start">
        <div className="lg:sticky lg:top-6">
          <Filters value={filter} onChange={setFilter} arms={arms} />
        </div>

        <ArchiveList spawns={shown} total={rows.length} />
      </div>
    </Page>
  );
}
