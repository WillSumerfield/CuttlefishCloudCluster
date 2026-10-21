import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { Arm } from "../api";
import { getFeatureCatalog, submit, submitTidepool } from "../api";
import { NumberField } from "./NumberField";
import { Chip } from "./Chip";
import { Select } from "./Select";
import { Tip } from "./Tip";

/**
 * The Spawn composer. `queue/SubmitComposer.tsx` is a thin wrapper over this.
 *
 * Name is required: a Spawn's is a label (CONTEXT.md, **Name**) — free text, capped, and two Spawns may
 * share one. It used to be optional here and to fall back to the script's name; it no longer falls
 * back to anything, because CCC names nothing.
 */

// Mirrors the Head's MAX_SPAWN_NAME (ccc_head_api.spawns), and only as a typing limit — the Head is
// what enforces it. Deliberately NOT paired with a charset check: a Spawn's
// **Name** is a label rather than a path segment, so there is nothing to escape and nothing to reject
// beyond the length and the control characters the Head strips out.
const MAX_SPAWN_NAME = 48;

// How many whole GB a card counts as — ROUNDED UP, mirroring `ccc_head_api.slurm.card_gb` and
// `cuttle.config.card_gb`. A card almost never reports the number it is sold as (ECC and
// virtualization both take a bite), so a 16 GB card can report 15.4 and must still answer `--vram 16`
// or a User is refused against hardware they can see. All three copies must agree: this one only
// decides what to grey out, but greying out what the Head would accept is the same bug seen earlier.
const cardGb = (vramMb: number) => Math.ceil(vramMb / 1024);

type SpawnProps = {
  arms: Arm[];
  isAdmin: boolean;
  onSubmitted: () => void;
};

export type ComposerProps = SpawnProps;

export function Composer(props: ComposerProps) {
  const spawn = props;

  // The container's own edge inset — space between ITS border and everything inside it (heading, strip,
  // every field), uniformly, not space between the container and whatever holds it (that's `mb-10`),
  // on top of the `var(--pad)` page-density base.
  const CONTAINER_PAD = { x: 50, y: 64 };

  const [mode, setMode] = useState<"script" | "tidepool">("script");
  const [name, setName] = useState("");
  const [script, setScript] = useState("");
  const [gpu, setGpu] = useState(1);
  const [minutes, setMinutes] = useState("");
  const [vram, setVram] = useState("");
  const [node, setNode] = useState("");
  const [args, setArgs] = useState("");
  const [featureCatalog, setFeatureCatalog] = useState<string[]>([]);
  const [features, setFeatures] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tidepoolId, setTidepoolId] = useState<number | null>(null);

  // The catalog is fetched once — what the Head knows exists.
  useEffect(() => {
    getFeatureCatalog()
      .then(setFeatureCatalog)
      .catch(() => setFeatureCatalog([]));
  }, []);

  const toggleFeature = (n: string) =>
    setFeatures((prev) => (prev.includes(n) ? prev.filter((f) => f !== n) : [...prev, n]));

  // --- how many Devices, and whether the fleet can host that many ---------------------------------
  //
  // The Devices one Spawn holds are always on a SINGLE Arm (CONTEXT.md), so every question below is
  // asked of one Arm at a time — never of the fleet's total card count.
  const arms = spawn.arms.filter((a) => a.admitted && !a.retracted);

  // Memory is a TOTAL, so the per-card need is the total divided by the count, rounded up. Exact
  // rather than approximate: a mixed-GPU box is refused at enroll, so every card on an Arm is the
  // same size. This is the same arithmetic slurm.vram_constraint does at submit.
  const perCard = vram ? Math.ceil(Number(vram) / Math.max(1, gpu)) : 0;

  // Both needs are checked TOGETHER, against one Arm. Asked separately they would leave a count
  // enabled that nothing can run: a 2×40GB box answers "two cards" and a 1×96GB box answers "90GB
  // each", and neither box can host the Spawn that wanted both.
  //
  // An empty `arms` means the fleet hasn't loaded, not that nothing can run — every count stays
  // pickable and the Head answers for real at submit. Greying out the whole control on a slow fetch
  // would read as "the cluster is broken".
  const known = arms.length > 0;
  const gpuOptions = Array.from(
    { length: Math.max(1, ...arms.map((a) => a.gpus)) },
    (_, i) => i + 1,
  ).map((n) => {
    const need = vram ? Math.ceil(Number(vram) / n) : 0;
    return {
      value: String(n),
      label: String(n),
      // `cardGb`, not a raw divide: this greys out the counts the Head would refuse, so it has to
      // measure a card exactly as the Head does (ccc_head_api.slurm.card_gb). Dividing exactly made a
      // 16 GB card reporting 15.4 fail `>= 16` here while the Head accepted it — a row greyed out over
      // a Spawn that would have run.
      disabled: known && !arms.some((a) => a.gpus >= n && cardGb(a.vram_mb ?? 0) >= need),
    };
  });

  // Clears the whole form and stays open, ready for another.
  const reset = () => {
    setName("");
    setScript("");
    setMinutes("");
    setVram("");
    setArgs("");
    setNode("");
    setFeatures([]);
  };

  const trimmed = name.trim();

  // The selected count can go stale without being re-picked: choose 2 cards at 40GB, then raise Memory
  // to 400, and the option that was fine is now one no Arm can host. Greying the row out doesn't undo
  // a choice already made, so readiness re-asks the same question of what is currently selected.
  // A count past the end of the list is unplaceable too, not merely unlisted.
  const placeable = !known || !!gpuOptions[gpu - 1] && !gpuOptions[gpu - 1].disabled;
  // A Tide Pool asks for a shell, not a run: no script.
  const tide = mode === "tidepool";

  // EVERY reason the primary button is dead, in the order the form asks for them — the Tip on that
  // button says them out loud, because a control that is simply inert teaches nobody what it wants.
  // `ready` is derived FROM this list rather than computed beside it: two expressions of one rule
  // drift, and the half that drifts silently is the explanation, which nothing type-checks.
  const blockers: string[] = [];
  // Every Spawn carries a **Name** a person chose (CONTEXT.md) — the Head refuses a nameless one, so
  // this gates exactly as the time and memory fields do. A Tide Pool is no exception.
  if (!trimmed) blockers.push(tide ? "name this tide pool" : "name this spawn");
  if (!tide && !script.trim()) blockers.push("name the script to run");
  if (!(Number(minutes) > 0)) blockers.push("set a time limit");
  if (!(Number(vram) > 0)) blockers.push("set the GPU memory it needs");
  // Already drawn elsewhere on the form (the greyed card rows). Said again here anyway: this list
  // answers "why is this dead", and an answer with a hole in it sends someone hunting the form for a
  // message they have already scrolled past.
  if (!placeable) blockers.push("lower the GPU count — no arm can host that many at this memory");

  const ready = blockers.length === 0;
  // One sentence, because `Tip` takes prose and not markup — a bulleted list of fields is documentation
  // growing inside a tooltip, which is the thing Tip's own contract refuses.
  const why = `Not ready yet: ${blockers.join("; ")}.`;

  // Wrapped ONLY while blocked: a live button explains itself, so a tip existing at all is itself the
  // signal that something is missing. `pointerEvents: none` is what lets the wrapper see the pointer —
  // a disabled <button> dispatches no pointer events and they do not reach an ancestor either, so a Tip
  // around one would be a hover explanation that never explains. The `not-allowed` cursor goes with it;
  // an explanation you can actually read is the better trade.
  const blocked = (btn: ReactNode) =>
    ready ? btn : <Tip text={why} className="justify-self-center" focusable={false}>{btn}</Tip>;

  const go = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      if (mode === "tidepool") {
        const id = await submitTidepool({
          minutes: Number(minutes), vram: Number(vram), gpu, node: node || null, features,
          // `trimmed`, never `|| null`: a Name is required now, and `ready` above has already refused
          // to enable the button without one. Sending null could only ever produce a 422 for a field
          // the form itself just guaranteed.
          name: trimmed,
        });
        setTidepoolId(id);
      } else {
        await submit({
          name: trimmed, script: script.trim(), gpu, minutes: Number(minutes),
          vram: Number(vram), node: node || null, args: args.split(/\s+/).filter(Boolean),
          features,
        });
      }
      reset();
      spawn.onSubmitted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const fields = (
    <div className="mt-6 space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <Labeled label="Name" className="sm:col-span-2" help={"What this run is called in the queue and the archive. Required — CCC won't name it for you, and a queue of jobs all called train.py is one nobody can read. Two runs may share a name."}>
          <input
            className="field"
            // MAX_SPAWN_NAME (ccc_head_api.spawns) for a Spawn. The Head is the one that enforces it;
            // this only stops you typing past the limit and meeting a 422 for it.
            maxLength={MAX_SPAWN_NAME}
            placeholder="lr sweep, 3e-4"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Labeled>

        {mode === "script" && (
          <>
            <Labeled label="Script" className="sm:col-span-2" help="The file the cluster actually runs.">
              <input className="field" placeholder="train.py" value={script} onChange={(e) => setScript(e.target.value)} />
            </Labeled>
            <Labeled label="Arguments" className="sm:col-span-2" help="Passed to your script exactly as typed, the same as if you had run it yourself.">
              <input className="field" placeholder="optional" value={args} onChange={(e) => setArgs(e.target.value)} />
            </Labeled>
          </>
        )}

        {featureCatalog.length > 0 && (
          <Labeled label="Required features" className="sm:col-span-2" help="Heavy or licence-gated software that isn't installed everywhere. Most runs need none. If no Arm in the fleet has what you ask for, the Head refuses the Spawn rather than queueing it forever.">
            <div className="flex flex-wrap gap-1.5">
              {featureCatalog.map((f) => (
                <Chip key={f} on={features.includes(f)} onClick={() => toggleFeature(f)}>
                  {f}
                </Chip>
              ))}
            </div>
          </Labeled>
        )}
        <Labeled label="Memory" help="TOTAL GPU memory you need, across the cards you asked for. Every card holding its share becomes eligible, and the fastest free set takes the job — asking for more than you need narrows the pool and makes you wait longer.">
          <NumberField value={vram} onChange={setVram} min={1} placeholder="VRAM, GB" ariaLabel="Memory, VRAM in GB" />
        </Labeled>
        <Labeled label="Time" help="How long the run may take before it is killed. Karma is escrowed against it, so over-estimating costs you queue position and nothing else — the difference is refunded when the Spawn ends.">
          <NumberField value={minutes} onChange={setMinutes} min={1} placeholder="minutes" ariaLabel="Time in minutes" />
        </Labeled>
        <Labeled label="GPUs" help="How many separate cards this run holds at once, always on ONE machine. Each is priced separately, and Memory above is the total across them. Counts no Arm in the fleet can host are greyed out.">
          <Select
            ariaLabel="GPUs"
            value={String(gpu)}
            onChange={(v) => setGpu(Number(v))}
            options={gpuOptions}
          />
          {/* Only past one card: at a single Device the total IS the per-card figure, so this would do
              nothing but restate the Memory field directly above it. */}
          {gpu > 1 && perCard > 0 && (
            <span className="mt-1 block text-[12px] text-[var(--muted)]">
              {gpu} × ≥{perCard} GB per card
            </span>
          )}
        </Labeled>
        {/* Naming a machine is an Admin power. */}
        {spawn.isAdmin && (
          <Labeled label="Arm" help="Admin only. Everyone else declares what they need and the cluster places the work — naming a machine is how a queue stops being fair.">
            <Select
              ariaLabel="Target an arm"
              value={node}
              onChange={setNode}
              options={[{ value: "", label: "Any eligible" }, ...spawn.arms.map((a) => ({ value: a.name, label: a.name }))]}
            />
          </Labeled>
        )}
      </div>

      {error && <p className="text-[13px] text-[var(--alarm)]">{error}</p>}

      {/* Primary sits dead-centre; the secondary hangs off its right without shifting it. */}
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3">
        <span aria-hidden />
        {blocked(
          <button className="btn-primary justify-self-center" onClick={go} disabled={!ready || busy}
                  style={{ pointerEvents: ready ? undefined : "none" }}>
            {busy
              ? mode === "tidepool" ? "Requesting…" : "Submitting…"
              : mode === "tidepool" ? "Request tide pool" : "Submit spawn"}
          </button>
        )}
        <button className="btn-ghost justify-self-start" onClick={reset} disabled={busy}>
          Clear
        </button>
      </div>
    </div>
  );

  return (
    <>
      {tidepoolId !== null && <TidepoolPopup id={tidepoolId} onClose={() => setTidepoolId(null)} />}
      <section
        className="surface [--sink:35%] mb-10"
        style={{
          padding: `calc(var(--pad) + ${CONTAINER_PAD.y}px) calc(var(--pad) + ${CONTAINER_PAD.x}px)`,
        }}
      >
        <div className="mb-5 flex gap-1">
          <Chip on={mode === "script"} onClick={() => setMode("script")}>Script</Chip>
          <Tip text="A live shell on a real GPU, for debugging against the hardware instead of guessing. It queues and costs Karma exactly like a Spawn, and it keeps holding the card until you cancel it or its time runs out — disconnecting does not end it.">
            <Chip on={mode === "tidepool"} onClick={() => setMode("tidepool")}>Tide Pool</Chip>
          </Tip>
        </div>
        {fields}
      </section>
    </>
  );
}

/**
 * Shown once, right after a Tide Pool is placed in the queue — the one moment the attach command
 * actually matters, so it carries the real id instead of living in compose-time text nobody reads
 * twice. The Dashboard's part ends here: the shell itself is CLI-only, no browser terminal.
 */
function TidepoolPopup({ id, onClose }: { id: number; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "color-mix(in oklab, var(--abyss) 75%, transparent)" }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="surface max-w-md space-y-4" style={{ padding: "calc(var(--pad) + 30px)" }}>
        <h2 className="text-[15px] font-semibold text-ink">Tide Pool requested — #{id}</h2>
        <p className="text-[13px] text-ink-soft">
          It's queued like any Spawn. Once it's running, attach from the CLI — the Dashboard has no
          browser terminal to open one from:
        </p>
        <p className="rounded-[10px] px-3 py-2 num text-[13px] text-ink" style={{ background: "color-mix(in oklab, var(--well) 70%, black)" }}>
          cuttle run --tidepool {id}
        </p>
        <p className="text-[13px] text-ink-soft">
          It holds the GPU and bills Karma from the moment it starts — whether or not you ever attach —
          until you cancel it or --time runs out:
        </p>
        <p className="rounded-[10px] px-3 py-2 num text-[13px] text-ink" style={{ background: "color-mix(in oklab, var(--well) 70%, black)" }}>
          cuttle cancel {id}
        </p>
        <div className="flex justify-end">
          <button className="btn-primary" onClick={onClose}>Got it</button>
        </div>
      </div>
    </div>
  );
}

/** A field's label, and its explanation on hover. */
function Labeled({ label, help, className, children }: { label: string; help?: string; className?: string; children: React.ReactNode }) {
  return (
    <div className={`block ${className ?? ""}`}>
      <span className="label mb-1.5 block">{label}</span>
      {help ? (
        <Tip text={help} className="block" focusable={false}>{children}</Tip>
      ) : (
        children
      )}
    </div>
  );
}
