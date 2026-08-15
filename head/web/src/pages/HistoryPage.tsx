import { useEffect, useMemo, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { session } from "../app/session";
import { getHistory, Unauthorized, type ArchivedSpawn } from "../api";
import { ArchiveList } from "../history/ArchiveList";
import { Filters, EMPTY_FILTER, type Filter } from "../history/Filters";

/**
 * The searchable archive of finished work. One flat, newest-first stream, filtered from a sidebar — the
 * inverse of the Queue: finished Spawns, no motion, no colour but the loud outcomes.
 *
 * It polls, slowly. This page used to fetch exactly once, on the reasoning that an archive is a record
 * rather than a live board — true of a row that has been there an hour, and false of the newest one. A
 * Spawn enters the archive the instant it leaves the queue, up to TICK_SECONDS (30s) BEFORE the tick
 * stamps its outcome and its cost, so the row a person is most likely to be watching is the one that is
 * guaranteed to be incomplete when it first appears. Fetched once, it stayed that way for as long as the
 * tab was open: `settling` forever, cost `—` forever, and before api.ts's outcomeOf was fixed, a red
 * `failed` on a Spawn that had exited 0 (observed on hardware 2026-08-15, Spawn 5, still wrong nine hours
 * later).
 *
 * 30s, matching the tick that stamps the rows — the slowest rate that still resolves every Spawn in one
 * poll. Deliberately far off the Queue's 4s: that board's whole job is to move.
 */
const POLL_MS = 30_000;

export function HistoryPage() {
  const { logout } = useAuth();
  const [rows, setRows] = useState<ArchivedSpawn[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [filter, setFilter] = useState<Filter>(EMPTY_FILTER);

  useEffect(() => {
    let alive = true;
    const refresh = () =>
      getHistory()
        .then((r) => {
          // The fetch can land after the page is gone (or after a later one) — dropping it keeps an
          // unmounted page from setting state and a slow response from overwriting a fresher one.
          if (alive) setRows(r);
        })
        .catch((e) => {
          // A session can expire while the archive is open; fall back to sign-in rather than a dead page.
          if (e instanceof Unauthorized) logout();
          // Any other error (Head unreachable, a 5xx) leaves the rows we have — a failed poll must never
          // empty an archive that was already on screen.
        })
        .finally(() => {
          if (alive) setLoaded(true);
        });
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
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
