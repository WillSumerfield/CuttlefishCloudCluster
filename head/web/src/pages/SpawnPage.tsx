import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Page } from "../app/Page";
import {
  outputsDownloadUrl,
  recoveryDownloadUrl,
  getLogs,
  getRecovery,
  getRetention,
  getSpawn,
  getStderr,
  Unauthorized,
  errMessage,
  type RecoveryStatus,
  type RetentionStatus,
  type SpawnDetail,
} from "../api";
import { useAuth } from "../app/auth";
import { session } from "../app/session";
import { Tip } from "../app/Tip";
import { human } from "../project";
import { CancelSpawn } from "../queue/CancelSpawn";
import { duration, armLabel, placedLabel } from "../queue/format";

/**
 * Recovery Window (docs/TODO.md): a 30-min, 16GB-capped safety net for whatever this Spawn wrote OUTSIDE
 * its declared outputs. Self-contained: fetches its own status and owns its own countdown, so it neither
 * waits on nor delays the page's main read. Renders nothing while loading or once there's nothing to show
 * — no "expired" state worth a User's attention after the fact.
 */
function RecoveryPanel({ id }: { id: string }) {
  const jobId = Number(id);
  const [status, setStatus] = useState<RecoveryStatus | null>(null);
  const [left, setLeft] = useState<number | null>(null);

  useEffect(() => {
    if (!Number.isFinite(jobId)) return;
    let alive = true;
    getRecovery(jobId).then((st) => alive && setStatus(st));
    return () => {
      alive = false;
    };
  }, [jobId]);

  useEffect(() => {
    if (!status) {
      setLeft(null);
      return;
    }
    const tick = () =>
      setLeft(Math.max(0, (new Date(status.expires_at).getTime() - Date.now()) / 3_600_000));
    tick();
    const timer = setInterval(tick, 15_000); // coarse — this is a courtesy countdown, not a live clock
    return () => clearInterval(timer);
  }, [status]);

  if (!status || left === null || left <= 0) return null;
  // Hours, and "<1h" for the last one — `recoveryLabel`'s shape (queue/format.ts) and the CLI hint's,
  // so all three surfaces describe one window identically. Never "1 day": see the Tip below.
  const remaining = left < 1 ? "<1h" : `${Math.round(left)}h`;

  return (
    <div className="shard mb-4 flex flex-wrap items-center justify-between gap-3">
      <div>
        <Tip
          className="block text-[14px] text-ink"
          text="Everything this run wrote outside the folders it declared, recoverable for a day after it ends and then gone. It exists for a mistake you didn't know you were making, so it's capped in size and not something to plan around."
        >
          Recovery Window open — <span className="num">{remaining}</span> left
        </Tip>
        <p className="mt-0.5 text-[12px] text-ink-mute">
          <span className="num">{human(status.bytes)}</span> this Spawn wrote outside its declared
          outputs, held on the Head as a safety net.
        </p>
      </div>
      {/* A link, not a fetch — the browser's download manager owns the transfer, so a 16 GB copy
          never sits in this tab. Only rendered when the Head said there is one, so the link cannot
          land on the route's 404. */}
      <a className="btn-primary" href={recoveryDownloadUrl(jobId)} download>
        Download recovery copy
      </a>
    </div>
  );
}

/**
 * The declared results, and how long the Head keeps them (docs/TODO.md "Storage retention"): 7 days
 * from the Spawn's end, or until the submitter's 32GB Allowance needs the room for newer ones.
 *
 * Deliberately the same panel shape as RecoveryPanel above (countdown, button) — different artifacts,
 * different deadlines, but one learned reading pattern. What differs is tone: this is what the Spawn was
 * submitted to produce, stated plainly rather than as a rescue. Renders nothing when nothing is held, and
 * no "expired" state — that would be the loudest thing on a page about a Spawn that ran fine.
 */
function OutputsPanel({ id }: { id: string }) {
  const jobId = Number(id);
  const [status, setStatus] = useState<RetentionStatus | null>(null);

  useEffect(() => {
    if (!Number.isFinite(jobId)) return;
    let alive = true;
    getRetention(jobId).then((st) => alive && setStatus(st));
    return () => {
      alive = false;
    };
  }, [jobId]);

  if (!status) return null;
  // Days, not minutes, and not live-ticking: a week-long deadline that re-renders every 15 seconds
  // would claim an urgency it does not have. The final day is where the wording earns its keep.
  const hours = (new Date(status.expires_at).getTime() - Date.now()) / 3_600_000;
  if (hours <= 0) return null;
  const left = hours < 24 ? `${Math.max(1, Math.round(hours))}h` : `${Math.round(hours / 24)}d`;

  return (
    <div className="shard mb-4 flex flex-wrap items-center justify-between gap-3">
      <div>
        <Tip
          className="block text-[14px] text-ink"
          text="What the run wrote into the folders it declared. Kept 7 days, or until your storage allowance needs the room for newer results."
        >
          Outputs kept for <span className="num">{left}</span> more
        </Tip>
        <p className="mt-0.5 text-[12px] text-ink-mute">
          <span className="num">{human(status.bytes)}</span> written to this Spawn's declared outputs.
          Fetch them before then — results are not kept forever.
        </p>
      </div>
      {/* See RecoveryPanel: a link, so a 3 GB result is the download manager's problem and not this
          tab's memory. This panel only renders when the retention route reported bytes held. */}
      <a className="btn-primary" href={outputsDownloadUrl(jobId)} download>
        Download outputs
      </a>
    </div>
  );
}

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

/**
 * Has this Spawn been placed on a machine — i.e. can a log file exist at all? Deliberately NOT
 * `verdict().live`, which tests /run|complet/ and so misses CONFIGURING: a Spawn whose nodes are booting
 * is allocated and about to write. This is `LiveQueue.isRunning`'s predicate, so a Spawn the queue calls
 * Running never tells its own page it has not started. A finished Spawn started by definition.
 */
function hasStarted(d: SpawnDetail): boolean {
  return d.done || /running|completing|configuring/i.test(d.state);
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

/** The knobs worth tuning on a log pane. Edit, save, watch. */
const LOG_PANE = {
  /**
   * The pool colour. Set inline, which is what makes it IMMOVABLE: `.field` recolours `--fill` on
   * hover and on focus, and an inline custom property outranks both. Lighter than a `.field`'s own
   * resting fill, because a log is read rather than typed into.
   */
  fill: "color-mix(in oklab, var(--well) 92%, var(--shoal))",

  /**
   * Edge feather, px — how far the fill dissolves at every rim before it is gone. Overrides the root
   * `--feather` (7, what every control shares) for this pane alone: a log is a wide reading surface, not
   * a control in a row of controls, so it melts further. Past ~24 on a short pane the two opposing ramps
   * start meeting in the middle and the whole fill goes translucent.
   */
  feather: 16,

  /** Corner radius, px — `.field`'s `--frad` (default 12). The feather rounds with it. */
  radius: 14,

  /** How tall a pane may grow before it scrolls internally rather than lengthening the page. */
  maxHeight: "28rem",

  /** Inner padding, px. Wants to comfortably clear `feather`, or the first glyph sits in the melt. */
  padX: 22,
  padY: 18,

  /** Line height of the mono text. Logs are scanned in columns, so a little air pays. */
  lineHeight: 1.6,
};

/**
 * One output stream. stdout and stderr get their own panes rather than being interleaved the way
 * `cuttle logs -f` does: the CLI interleaves because a terminal has one column, but the two files carry
 * no shared timestamps, so a merge here would be a guess presented as fact. The Environment build's own
 * output is not a third pane: the Arm builds it inside the job now, so it IS this Spawn's stderr.
 *
 * Empty is stated, not blank: "nothing on stderr" is good news on a Spawn that ran fine; an empty black
 * box says nothing at all. But it is only good news once something HAS run — a Spawn still waiting for a
 * machine returns the same "" from `/logs` (the sbatch file does not exist yet), and reading that as
 * "nothing on stdout" claims it ran and printed nothing. `started` tells the two apart. It is the third
 * collision on this empty string; main.py's `_SWEPT` is the Head's answer to the second.
 *
 * While unfinished the pane FOLLOWS: pinned to the newest line as each poll appends, like `cuttle logs -f`.
 * Scrolling up releases it and it holds still — a log that kept yanking you back would make a long
 * traceback unreadable — and scrolling back to the bottom re-arms it. A finished pane never follows and
 * opens at the top: it is a record, read from the start. A Spawn that finishes while you are watching
 * keeps whatever scroll position it had rather than jumping anywhere.
 */
function LogPane({
  title,
  text,
  live,
  started,
}: {
  title: string;
  text: string | null;
  live: boolean;
  started: boolean;
}) {
  const pane = useRef<HTMLPreElement | null>(null);
  const [following, setFollowing] = useState(true);

  // Layout effect, not effect: pin before the browser paints, or every poll flashes the old scroll
  // position for a frame. `live` gates it so a finished Spawn is never scrolled for you.
  useLayoutEffect(() => {
    const el = pane.current;
    if (!el || !live || !following) return;
    el.scrollTop = el.scrollHeight;
  }, [text, live, following]);

  // 8px of slack: a pane scrolled to the bottom can sit a subpixel short of it, and demanding exact
  // equality would drop you out of following the moment you touched the wheel.
  const onScroll = () => {
    const el = pane.current;
    if (el) setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight < 8);
  };

  // Only a live pane has a state worth naming. The note used to read "updating every 3s", which said
  // what the PAGE does rather than what you are looking at — and overstated freshness besides, since a
  // remote Arm's out/err ride a 5s rsync (spawns.py push_periodic) on top of the 3s poll.
  const note = live ? (following ? "following" : "paused — scroll down to resume") : undefined;

  return (
    <div className="min-w-0">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <Tip
          className="label"
          text="Kept as two streams rather than merged, because they carry no shared timestamps and interleaving them would be a guess. A crash's traceback lands in stderr — as does the Environment build that runs before it."
        >
          {title}
        </Tip>
        {note && <span className="text-[12px] text-ink-mute">{note}</span>}
      </div>
      {/* `field` — the same recess the composer's inputs sit in, feathering at every edge so a long log
          dissolves instead of ending on a hard rim. Vars set INLINE so they beat `.field`'s own
          `&:hover { --fill: … }` and hold the colour still: a log is not a control, so lighting up under
          the pointer would promise an interaction that doesn't exist. */}
      <pre
        ref={pane}
        onScroll={onScroll}
        className="field num overflow-auto text-[12px] whitespace-pre"
        style={{
          color: "var(--ink-soft)",
          "--fill": LOG_PANE.fill,
          "--feather": `${LOG_PANE.feather}px`,
          "--frad": `${LOG_PANE.radius}px`,
          maxHeight: LOG_PANE.maxHeight,
          padding: `${LOG_PANE.padY}px ${LOG_PANE.padX}px`,
          lineHeight: LOG_PANE.lineHeight,
        } as React.CSSProperties}
      >
        {text === null
          ? "…"
          : text.trim() !== ""
            ? text
            : started
              ? `nothing on ${title.toLowerCase()}`
              : "not started — no output yet"}
      </pre>
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
  const [out, setOut] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const timer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [d, o, e] = await Promise.all([
        getSpawn(jobId), getLogs(jobId), getStderr(jobId),
      ]);
      setDetail(d);
      setOut(o);
      setErr(e);
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
  // Cancel is for an OUTSTANDING Spawn — queued or running, `done` says which. Your own only: an Admin
  // may end anyone's, but that road is `cuttle cancel <id>` on the Head, not this button — the API's
  // ownership test is unconditional and gets no admin branch (docs/Security.md). The row you clicked in
  // from can still be stopped from here.
  const canCancel =
    !!detail && !detail.done && detail.submitted_by === session.username;

  return (
    <Page
      // Titled by its **Name**, like every list that links here — a Spawn page headed by the entry
      // script disagreed with the queue row you clicked to reach it, and said nothing at all for a
      // Workspace visit, which has no script.
      title={detail?.name || detail?.script || id}
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
              text="One run of one piece of work. It runs exactly once — if the machine it landed on dies, the Spawn dies with it rather than quietly starting again somewhere else."
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

      <OutputsPanel id={id} />
      <RecoveryPanel id={id} />

      {detail && (
        <div className="grid gap-8 lg:grid-cols-[minmax(260px,320px)_1fr] lg:items-start">
          {/* What it was, and what it cost — the column of facts, sticky beside the logs you scroll. */}
          {/* Same plate as the submit composer it visually rhymes with: --sink 35%, and a fade sized to
              the card rather than the 50px default, which on a narrow column is most of the column. */}
          <section
            className="surface [--sink:35%] [--fade-x:18px] [--fade-y:18px] lg:sticky lg:top-6"
            style={{ padding: "calc(var(--pad) + 18px)" }}
          >
            <Fact label="Submitted by" value={detail.submitted_by} />
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
            <Fact label="Declared" help="The time limit you asked for at submit, not how long it actually ran." value={duration(detail.minutes ?? null)} />
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
            <LogPane title="stdout" text={out} live={!detail.done} started={hasStarted(detail)} />
            <LogPane title="stderr" text={err} live={!detail.done} started={hasStarted(detail)} />
          </section>
        </div>
      )}
    </Page>
  );
}
