import { useCallback, useEffect, useRef, useState } from "react";
import { Page } from "../app/Page";
import { getDive, Unauthorized, errMessage, type DiveDetail } from "../api";
import { useAuth } from "../app/auth";
import { session } from "../app/session";
import { Tip } from "../app/Tip";
import { EndDive } from "../queue/EndDive";
import { duration, armLabel, placedLabel } from "../queue/format";

/** How a Dive's state should read, and whether it is worth colour. Only `lost` is loud — an ended or
 *  timed-out Dive is a decision or a limit, not a fault. */
function verdict(d: DiveDetail): { word: string; color: string; live: boolean } {
  if (d.state === "lost") return { word: "lost", color: "var(--alarm)", live: false };
  if (d.outcome) return { word: d.outcome, color: "var(--ink-mute)", live: false };
  return { word: d.state, color: "var(--irid-dim)", live: /run|complet/i.test(d.state) };
}

/** A fact and its name, in the column of them. `—` for what the Head does not know rather than a gap:
 *  an absent row would leave the reader wondering whether the fact exists or merely wasn't fetched. */
function Fact({ label, value, help }: { label: string; value: React.ReactNode; help?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      {help ? <Tip className="label" text={help}>{label}</Tip> : <span className="label">{label}</span>}
      <span className="num text-[13px] text-ink-soft">{value ?? "—"}</span>
    </div>
  );
}

/**
 * How to reach your own live Dive: `cuttle dive attach`, once an sshd answers (editors: the alias).
 * Queued or still staging says so. Absent for anyone else's Dive (the Head sends no connection).
 */
function Connect({ d }: { d: DiveDetail }) {
  if (d.ready === undefined || d.outcome || d.state === "ending") return null;
  return (
    <div className="surface space-y-3" style={{ padding: "calc(var(--pad) + 12px)" }}>
      <Tip className="label" text={`Your own keys and your Tidepool's host key, so \`cuttle login\` already set this box up. VS Code: Remote-SSH, host ${d.alias}.`}>
        Connect
      </Tip>
      {d.ready ? (
        <>
          <pre className="field num text-[13px]" style={{ padding: "12px 18px", color: "var(--ink)" }}>
            {`cuttle dive attach ${d.id}`}
          </pre>
          <p className="text-[13px] text-ink-soft">
            {`On ${d.node}: ~/${d.main} is mirrored back to your Tidepool every minute, and is read-only there until the Dive ends.`}
          </p>
        </>
      ) : (
        <p className="text-[13px] text-ink-soft">
          {d.state === "pending" ? "Queued — waiting for a GPU." : d.state === "running" ? "Staging your folders…" : d.state}
        </p>
      )}
    </div>
  );
}

/**
 * One Dive, reached from Queue, History, Karma or the Ledger. Not a rail destination.
 *
 * One fetch answers the whole page — `GET /dives/{id}` carries the live state, the archive row and,
 * for its owner, the connection —
 * so it never has to know whether its subject is queued, running or a year old. Polls only while
 * unfinished: an archive page hitting the Head every few seconds would be asking a question whose answer
 * cannot change.
 */
export function DivePage({ id }: { id: string }) {
  const jobId = Number(id);
  const { logout } = useAuth();
  const [detail, setDetail] = useState<DiveDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const incomingNotes = window.history.state?.diveNotes;
  const notes: string[] = Array.isArray(incomingNotes)
    ? incomingNotes.filter((v): v is string => typeof v === "string") : [];
  const [confirming, setConfirming] = useState(false);
  const timer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await getDive(jobId);
      setDetail(d);
      return d.done;
    } catch (e) {
      if (e instanceof Unauthorized) logout();
      else setError(errMessage(e));
      return true; // stop polling something that isn't answering
    }
  }, [jobId, logout]);

  useEffect(() => {
    if (!Number.isFinite(jobId)) return;
    let alive = true;
    const tick = async () => {
      const done = await refresh();
      // Re-armed each pass rather than set on an interval, so a Dive that finishes stops the polling
      // on the very next tick instead of one interval later.
      if (alive && !done) timer.current = window.setTimeout(tick, 3000);
    };
    tick();
    return () => {
      alive = false;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [jobId, refresh]);

  const v = detail ? verdict(detail) : null;
  // End is for an OUTSTANDING Dive — queued or running, `done` says which. Your own only: an Admin
  // may end anyone's, but that road is `cuttle dive end <id>` on the Head, not this button — the API's
  // ownership test is unconditional and gets no admin branch (docs/Security.md). The row you clicked in
  // from can still be stopped from here.
  const canCancel =
    !!detail && !detail.outcome && detail.state !== "ending" && detail.started_by === session.username;

  return (
    <Page
      title={detail?.main ? `~/${detail.main}` : `Dive ${id}`}
      actions={
        v ? (
          <span className="flex items-center gap-2 text-[13px]">
            {v.live && (
              <span
                className="h-1.5 w-1.5 rounded-full"
                style={{ background: v.color, animation: "breathe 3.2s ease-in-out infinite" }}
              />
            )}
            <Tip
              text="A GPU session over folders of a Tidepool. It ends at its time limit or when its owner ends it — disconnecting never does."
              className=""
            >
              <span style={{ color: v.color }}>{v.word}</span>
            </Tip>
            <span className="num text-ink-mute">#{id}</span>
            {/* No optimistic state here: the page is already polling itself, so one extra read is enough
                to turn the verdict to "cancelled" and take this away with it. */}
            {canCancel && (
              <EndDive
                id={jobId}
                confirming={confirming}
                setConfirming={setConfirming}
                onCancelled={() => void refresh()}
              />
            )}
          </span>
        ) : null
      }
    >
      {error && <p className="mb-6 text-[13px]" style={{ color: "var(--alarm)" }}>{error}</p>}
      {notes.map((note, i) => <p key={i} role="status" className="mb-4 text-[13px] text-ink-soft">{note}</p>)}


      {detail && (
        <div className="grid gap-8 lg:grid-cols-[minmax(260px,320px)_1fr] lg:items-start">
          {/* What it was, and what it cost — the column of facts, sticky beside the logs you scroll. */}
          {/* --sink 35%, and a fade sized to the card rather than the 50px default, which on a narrow
              column is most of the column. */}
          <section
            className="surface [--sink:35%] [--fade-x:18px] [--fade-y:18px] lg:sticky lg:top-6"
            style={{ padding: "calc(var(--pad) + 18px)" }}
          >
            <Fact label="Started by" value={detail.started_by} />
            <Fact label="Main folder" help="Mirrored back to its owner's Tidepool; read-only there while the Dive holds it." value={detail.main ? `~/${detail.main}` : null} />
            {/* Where it RAN — and the two words for "we don't know" differ on purpose: a queued Dive is
                genuinely unplaced, a finished one was placed and simply ended too fast to be metered. */}
            <Fact
              label="Arm"
              value={detail.outcome ? placedLabel(detail.placed_node) : armLabel(detail.placed_node)}
            />
            <Fact label="GPUs" help="Cards held at once, always on the one machine." value={detail.gpus} />
            <Fact
              label="VRAM"
              help="What was asked for — the total across those cards, not the size of each."
              value={detail.vram_gb ? `${detail.vram_gb} GB` : null}
            />
            <Fact label="Time limit" help="What was asked for (--time), not how long it actually ran." value={duration(detail.minutes ?? null)} />
            <Fact
              label="Started"
              value={detail.submitted_at ? new Date(detail.submitted_at).toLocaleString() : null}
            />
            {detail.outcome && (
              <Fact
                label="Finished"
                value={detail.finished_at ? new Date(detail.finished_at).toLocaleString() : null}
              />
            )}
            {/* Cost is the only row that can still change after the Dive ends: null until Settlement
                writes the ledger row. Plain ink, never pigment — pigment means standing. */}
            <Fact
              label="Cost"
              help="What this run actually settled at: the seconds it held the card, priced by what that card can do and how badly it was wanted at the time. You pay for holding it, not for keeping it busy."
              value={
                detail.cost === null || detail.cost === undefined
                  ? detail.outcome ? "not settled" : "still metering"
                  : `${Math.round(detail.cost)} karma`
              }
            />
          </section>

          <section className="min-w-0 space-y-8">
            <Connect d={detail} />
          </section>
        </div>
      )}
    </Page>
  );
}
