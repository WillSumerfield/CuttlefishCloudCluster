import { useCallback, useEffect, useRef, useState } from "react";
import { Page } from "../app/Page";
import {
  getDiveConn,
  getSpawn,
  Unauthorized,
  errMessage,
  type DiveConn,
  type SpawnDetail,
} from "../api";
import { useAuth } from "../app/auth";
import { session } from "../app/session";
import { Tip } from "../app/Tip";
import { CancelSpawn } from "../queue/CancelSpawn";
import { duration, armLabel, placedLabel } from "../queue/format";

/** How a Spawn's state should read, and whether it is worth colour. Only a real failure is loud — a
 *  cancelled Spawn is a decision somebody made, not a fault, and drawing it in alarm would say otherwise. */
function verdict(d: SpawnDetail): { word: string; color: string; live: boolean } {
  if (!d.done) return { word: d.state, color: "var(--irid-dim)", live: /run|complet/i.test(d.state) };
  const rc = d.exit ?? d.exit_code;
  if (d.state === "cancelled") return { word: "cancelled", color: "var(--ink-mute)", live: false };
  if (d.state === "failed") return { word: "failed", color: "var(--alarm)", live: false };
  if (rc === 0) return { word: "done", color: "var(--ink-soft)", live: false };
  return { word: `failed · exit ${rc}`, color: "var(--alarm)", live: false };
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
 * Queued or still staging says so; a Head Dive is the Tidepool itself, holding the GPU.
 */
function Connect({ conn }: { conn: DiveConn | null }) {
  if (!conn) return null;
  return (
    <div className="surface space-y-3" style={{ padding: "calc(var(--pad) + 12px)" }}>
      <Tip className="label" text={`Your own keys and your Tidepool's host key, so \`cuttle login\` already set this box up. VS Code: Remote-SSH, host ${conn.alias}.`}>
        Connect
      </Tip>
      {conn.ready ? (
        <>
          <pre className="field num text-[13px]" style={{ padding: "12px 18px", color: "var(--ink)" }}>
            {`cuttle dive attach ${conn.id}`}
          </pre>
          <p className="text-[13px] text-ink-soft">
            {conn.head
              ? "On the Head: your Tidepool itself has the GPU until this Dive ends."
              : `On ${conn.node}: ~/${conn.main} is mirrored back to your Tidepool every minute, and is read-only there until the Dive ends.`}
          </p>
        </>
      ) : (
        <p className="text-[13px] text-ink-soft">
          {conn.state === "pending" ? "Queued — waiting for a GPU." : conn.state === "running" ? "Staging your folders…" : conn.state}
        </p>
      )}
    </div>
  );
}

/**
 * One Spawn, reached from Queue, History, Karma or the Ledger. Not a rail destination.
 *
 * One fetch answers the whole page — `GET /spawns/{id}` carries the live verdict AND the archive row —
 * so it never has to know whether its subject is queued, running or a year old. Polls only while
 * unfinished: an archive page hitting the Head every few seconds would be asking a question whose answer
 * cannot change.
 */
export function SpawnPage({ id }: { id: string }) {
  const jobId = Number(id);
  const { logout } = useAuth();
  const [detail, setDetail] = useState<SpawnDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conn, setConn] = useState<DiveConn | null>(null);
  const [confirming, setConfirming] = useState(false);
  const timer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await getSpawn(jobId);
      setDetail(d);
      setConn(!d.done && d.submitted_by === session.username ? await getDiveConn(jobId) : null);
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
      // Re-armed each pass rather than set on an interval, so a Spawn that finishes stops the polling
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
    !!detail && !detail.done && detail.submitted_by === session.username;

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
              <CancelSpawn
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


      {detail && (
        <div className="grid gap-8 lg:grid-cols-[minmax(260px,320px)_1fr] lg:items-start">
          {/* What it was, and what it cost — the column of facts, sticky beside the logs you scroll. */}
          {/* --sink 35%, and a fade sized to the card rather than the 50px default, which on a narrow
              column is most of the column. */}
          <section
            className="surface [--sink:35%] [--fade-x:18px] [--fade-y:18px] lg:sticky lg:top-6"
            style={{ padding: "calc(var(--pad) + 18px)" }}
          >
            <Fact label="Submitted by" value={detail.submitted_by} />
            <Fact label="Main folder" help="Mirrored back to its owner's Tidepool; read-only there while the Dive holds it." value={detail.main ? `~/${detail.main}` : null} />
            {/* Where it RAN once there is such a thing, falling back to the Admin's demand — and the
                two words for "we don't know" are different on purpose: a queued Spawn is genuinely
                unplaced, a finished one was placed and simply ended too fast to be metered. */}
            <Fact
              label="Arm"
              value={
                detail.done
                  ? placedLabel(detail.placed_node ?? detail.node ?? null)
                  : armLabel(detail.placed_node ?? detail.node ?? null)
              }
            />
            <Fact label="GPUs" help="Cards held at once, always on the one machine." value={detail.gpus} />
            <Fact
              label="VRAM"
              help="What was asked for — the total across those cards, not the size of each."
              value={detail.vram_gb ? `${detail.vram_gb} GB` : null}
            />
            <Fact label="Time limit" help="What was asked for (--time), not how long it actually ran." value={duration(detail.minutes ?? null)} />
            <Fact
              label="Submitted"
              value={detail.submitted_at ? new Date(detail.submitted_at).toLocaleString() : null}
            />
            {detail.done && (
              <Fact
                label="Finished"
                value={detail.finished_at ? new Date(detail.finished_at).toLocaleString() : null}
              />
            )}
            {/* Cost is the last row and the only one that can still change after the Spawn ends: it is
                null until Settlement writes the ledger row, and an unsettled Spawn says so rather than
                showing a zero it would later contradict. Plain ink, never pigment — pigment means
                standing, and what a Spawn cost is not a judgement of who ran it. */}
            <Fact
              label="Cost"
              help="What this run actually settled at: the seconds it held the card, priced by what that card can do and how badly it was wanted at the time. You pay for holding it, not for keeping it busy."
              value={
                detail.cost === null || detail.cost === undefined
                  ? detail.done ? "not settled" : "still metering"
                  : `${Math.round(detail.cost)} karma`
              }
            />
          </section>

          <section className="min-w-0 space-y-8">
            <Connect conn={conn} />
          </section>
        </div>
      )}
    </Page>
  );
}
