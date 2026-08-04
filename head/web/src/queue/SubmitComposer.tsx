import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Arm } from "../api";
import { getFeatureCatalog, getWandbKey, submit, submitTidepool } from "../api";
import { human, pick, relPath, type Picked } from "../project";
import { Select } from "../app/Select";
import { NumberField } from "../app/NumberField";
import { Chip } from "../app/Chip";
import { Tip } from "../app/Tip";
import { Link } from "../app/router";

// Mirrors the Head's OUTPUT_NAME_RE (ccc_head_api.spawns) — an output name becomes a shell-interpolated
// `mkdir -p "$ccc_out/<name>"`, so the same "no slashes, no leading dot" rule is enforced here too. A
// client-side reject is instant; the Head's is the one that actually matters (it never trusts this one).
const OUTPUT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

/**
 * Submit a Spawn. The affordance IS the form: an empty strip that says "drop a project or choose a
 * folder", and picking one expands the composer inline — the expansion is also the PREVIEW GATE (name,
 * file count, total size). Nothing uploads until Submit; bytes move on one click, never on the pick.
 *
 * Source is folder today; a Recipe is another way in (Ph5) into the same composer, so the picked
 * project is held behind `source` rather than assumed to be a folder everywhere.
 *
 * Two MODES (docs/TODO.md "Tide Pool"): "Script" is everything above. "Tide Pool" is a live interactive
 * shell instead — hides the project/upload story entirely and submits only the declared resource needs.
 * Shell access is CLI-only (`cuttle run --tidepool <id>`); the Dashboard just requests one and shows it
 * in the queue once placed.
 */

const GB = 1024 ** 3;

/* Drop strip's soft-edged pool: a ROUNDED RECT feathered by a blur, on a layer behind the label text so
 * the fill melts at the rim while copy stays crisp. NOT the surfaces' crossed-linear-ramp mask: that
 * feathers SQUARE corners and its dead-zone swallows a rounded corner until the radius outgrows it, so
 * `radius` did nothing visible there. A blur feathers the true perimeter instead. Edit, save, watch. */
const DROP_FADE = {
  /** Corner radius of the recess, px. */
  radius: 16,

  /** Soft-edge feather, px (blur radius). The rim dissolves over roughly 2× this. */
  feather: 8,
};

export function SubmitComposer({
  arms,
  isAdmin,
  startOpen,
  onSubmitted,
}: {
  arms: Arm[];
  isAdmin: boolean;
  /** Open on load when the queue is empty — an empty screen is an invitation to act. */
  startOpen?: boolean;
  onSubmitted: () => void;
}) {
  // "Batch" was rejected as a label for the script mode — this stays a plain binary toggle, no
  // implied third state.
  const [mode, setMode] = useState<"script" | "tidepool">("script");
  const [picked, setPicked] = useState<Picked | null>(null);
  const [honor, setHonor] = useState(true);
  const [name, setName] = useState("");
  const [script, setScript] = useState("");
  const [gpu, setGpu] = useState(1);
  const [minutes, setMinutes] = useState("");
  const [vram, setVram] = useState("");
  const [node, setNode] = useState("");
  const [args, setArgs] = useState("");
  // Named subdirs under $CCC_OUT (docs/TODO.md's now-closed "Spawn Output Enforcement") — starts EMPTY,
  // no pre-fill, and gates `ready` below just like `picked`/`script` already do.
  const [outputs, setOutputs] = useState<string[]>([]);
  const [outputDraft, setOutputDraft] = useState("");
  // Arm Features (docs/TODO.md) this Spawn requires — a picker over what the Head supports, never
  // free-typed (unlike outputs above): the catalog is built into CCC, so a name outside it is one no
  // Arm could carry. Starts empty and stays optional; the Head 422s only an UNSATISFIABLE request.
  const [featureCatalog, setFeatureCatalog] = useState<string[]>([]);
  const [features, setFeatures] = useState<string[]>([]);
  // Which dependency file builds this Spawn's Python environment (docs/TODO.md "Python Environments").
  // Blank is the normal case and means "look in the project root yourself" — auto-detection is the
  // Head's job and it sees the uploaded tree, which this composer only sees a file list of. Free-typed
  // rather than a picker over `picked`: the answer may be a file the User is about to add, and "none"
  // is a valid answer that is not a path at all.
  const [manifest, setManifest] = useState("");
  // What the Head says it will build, echoed after a successful submit. Held here rather than in a
  // toast because it is the answer to a question the User has already stopped thinking about — a
  // manifest one directory too deep is not an error anywhere, and this line is the only mention it
  // will ever get.
  const [envNote, setEnvNote] = useState<string | null>(null);
  // Log THIS Spawn, default on: a stored key would otherwise log every Spawn a User submits, with no
  // way to keep a throwaway out of the project's run history.
  const [wandbLog, setWandbLog] = useState(true);
  const [wandbProject, setWandbProject] = useState("");
  const [wandbEntity, setWandbEntity] = useState("");
  // Whether W&B is on for this User at all — null until the Head answers. Asking two questions about a
  // run that will never be logged is the same silent failure this feature was built to end, so the
  // fields wait for the answer rather than guess. A FAILED read shows them: never block submitting a
  // Spawn on a status line about metrics.
  const [wandbOn, setWandbOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The id of a just-requested Tide Pool, or null when there's no popup to show. Set once, right after
  // submitTidepool resolves — this is the one moment a User actually needs the attach command, so it's
  // handed to them directly instead of left in the compose-time text for them to remember later.
  const [tidepoolId, setTidepoolId] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const lastList = useRef<FileList | null>(null);

  useEffect(() => {
    getWandbKey()
      .then((s) => setWandbOn(s.set))
      .catch(() => setWandbOn(true)); // unreadable → assume on; the Head decides for real at submit
  }, []);

  // The catalog is fetched once, not tied to any picked project — it's what the Head knows exists,
  // independent of what's being submitted. A failed read just leaves the field empty (nothing to
  // require) rather than blocking the composer over an optional field.
  useEffect(() => {
    getFeatureCatalog()
      .then(setFeatureCatalog)
      .catch(() => setFeatureCatalog([]));
  }, []);

  const toggleFeature = (name: string) =>
    setFeatures((prev) => (prev.includes(name) ? prev.filter((f) => f !== name) : [...prev, name]));

  // The script picker's choices: every .py in the picked tree, so "script must exist" is true by
  // construction — there is no free-text path to typo. Sorted so a re-pick doesn't reshuffle the list.
  const scriptOptions = useMemo(
    () => (picked ? picked.files.filter((f) => f.name.endsWith(".py")).map(relPath).sort() : []),
    [picked],
  );

  // A repick (different folder, or the same one re-scanned with .gitignore toggled) can drop the
  // previously-selected script from the tree entirely. Clearing it here — rather than letting the
  // dropdown silently fall back to its first option while `script` state still held the stale path —
  // is what keeps the submitted value in sync with what the control actually shows.
  useEffect(() => {
    if (script && !scriptOptions.includes(script)) setScript("");
  }, [scriptOptions, script]);

  const rescan = useCallback(async (list: FileList | null, honorGitignore: boolean) => {
    if (!list || list.length === 0) return;
    setPicked(await pick(list, honorGitignore));
  }, []);

  const onPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    lastList.current = e.target.files;
    setError(null);
    await rescan(e.target.files, honor);
  };

  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files?.length) {
      lastList.current = e.dataTransfer.files;
      setError(null);
      await rescan(e.dataTransfer.files, honor);
    }
  };

  const toggleHonor = async (v: boolean) => {
    setHonor(v);
    await rescan(lastList.current, v);
  };

  const reset = () => {
    setPicked(null);
    setName("");
    setScript("");
    setMinutes("");
    setVram("");
    setArgs("");
    setNode("");
    setWandbLog(true);
    setWandbProject("");
    setWandbEntity("");
    setOutputs([]);
    setOutputDraft("");
    setFeatures([]);
    setManifest("");
    // `envNote` is deliberately NOT cleared: it describes the Spawn that was just submitted, and this
    // reset is what clears the form out from under it. Cleared when the next submit starts instead.
    if (fileRef.current) fileRef.current.value = "";
  };

  const addOutput = () => {
    const name = outputDraft.trim();
    if (!name) return;
    if (!OUTPUT_NAME_RE.test(name)) {
      setError(`"${name}" isn't a valid output name — letters, digits, '.', '_', '-' only, and it can't start with '.'`);
      return;
    }
    setError(null);
    setOutputs((prev) => (prev.includes(name) ? prev : [...prev, name]));
    setOutputDraft("");
  };

  const removeOutput = (name: string) => setOutputs((prev) => prev.filter((o) => o !== name));

  const ready =
    mode === "tidepool"
      ? Number(minutes) > 0 && Number(vram) > 0
      : picked && script && Number(minutes) > 0 && Number(vram) > 0 && outputs.length > 0;

  const go = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    setEnvNote(null);
    try {
      if (mode === "tidepool") {
        const id = await submitTidepool({
          minutes: Number(minutes),
          vram: Number(vram),
          gpu,
          node: node || null,
          features,
          picked,
          name: name.trim() || null,
        });
        setTidepoolId(id);
      } else {
        const res = await submit({
          picked: picked as Picked,
          name: name.trim() || null,
          script,
          gpu,
          minutes: Number(minutes),
          vram: Number(vram),
          node: node || null,
          args: args.split(/\s+/).filter(Boolean),
          wandb: wandbLog,
          wandbProject,
          wandbEntity,
          outputs,
          features,
          manifest,
        });
        setEnvNote(res.environment);
      }
      reset();
      onSubmitted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const open = startOpen || picked !== null || mode === "tidepool";

  return (
    <>
    {tidepoolId !== null && <TidepoolPopup id={tidepoolId} onClose={() => setTidepoolId(null)} />}
    <section className="surface [--sink:35%] mb-10" style={{ padding: "calc(var(--pad) + 50px) calc(var(--pad) + 50px)" }}>
      {/* Script vs Tide Pool. A plain toggle, not a picker — there are exactly two shapes a Spawn can
          take here, and Tide Pool hides everything below that only means something for a project. */}
      <div className="mb-5 flex gap-1">
        <Chip on={mode === "script"} onClick={() => setMode("script")}>Script</Chip>
        <Tip text="A live shell on a real GPU, for debugging against the hardware instead of guessing. It queues and costs Karma exactly like a Spawn, and it keeps holding the card until you cancel it or its time runs out — disconnecting does not end it.">
          <Chip on={mode === "tidepool"} onClick={() => setMode("tidepool")}>Tide Pool</Chip>
        </Tip>
      </div>

      {(
        // The strip — a recess you drop a folder onto, or click to choose one. Shown in both modes: a
        // Tide Pool's project is optional (it lands at $proj on the Arm for the live shell to use, never
        // run automatically), where Script's is required.
        <label
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className="relative flex cursor-pointer items-center justify-center px-6 py-7 text-center"
          // minHeight ties the strip to the radius: a corner radius clamps to half the box's shorter side,
          // so on a short strip anything past ~half-height stopped rounding. `radius * 2` guarantees the box
          // is always tall enough for the full radius to render (content padding still wins when taller).
          style={{ borderRadius: DROP_FADE.radius, minHeight: DROP_FADE.radius * 2 }}
        >
          {/* The fill, on its own layer, feathered to nothing at every edge by a blur — the recess has no
              hard rounded rim, only a soft-edged pool of lower luminance you drop onto. */}
          <span
            aria-hidden
            className="absolute inset-0 transition-[background] duration-200"
            style={{
              borderRadius: DROP_FADE.radius,
              background: dragging
                ? "color-mix(in oklab, var(--irid-low) 14%, var(--well))"
                : "color-mix(in oklab, var(--well) 70%, black)",
              filter: `blur(${DROP_FADE.feather}px)`,
            }}
          />
          <input
            ref={fileRef}
            type="file"
            onChange={onPick}
            className="sr-only"
            {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
          />
          <span className={`relative text-[15px] ${picked ? "text-ink-soft" : "text-ink-mute"}`}>
            {picked ? (
              <>
                <span className="num text-ink">{picked.root}</span>
              </>
            ) : mode === "tidepool" ? (
              <>Drop/choose a folder — optional</>
            ) : (
              <>Drop/choose a folder</>
            )}
          </span>
        </label>
      )}

      {open && (
        <div className="mt-6 space-y-5">
          {/* The preview gate. Everything the pick told us, and the promise that nothing has moved. */}
          {picked && (
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 px-1 text-[13px]">
              <span className="text-ink-soft">
                <span className="num text-ink">{picked.files.length}</span>/
                <span className="num">{picked.scanned}</span> files,{" "}
                <span className="num text-ink">{human(picked.bytes)}</span>
              </span>
              <div className="ml-auto flex gap-1">
                <Chip on={!honor} onClick={() => toggleHonor(false)}>All files</Chip>
                <Chip on={honor} onClick={() => toggleHonor(true)}>Follow .gitignore</Chip>
              </div>
            </div>
          )}

          {picked && picked.bytes > GB && (
            <p className="rounded-[10px] px-3 py-2 text-[13px]" style={{ background: "color-mix(in oklab, var(--alarm) 12%, transparent)", color: "var(--ink-soft)" }}>
              That's over a gigabyte. Check your .gitignore excludes data and checkpoints — the upload
              caps at 4&nbsp;GB.
            </p>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <Labeled label="Name" help="An optional label so you can recognise this run in the queue later. Leave it blank and the script's name is used." className="sm:col-span-2">
              <input className="field" placeholder="optional" value={name} onChange={(e) => setName(e.target.value)} />
            </Labeled>
            {mode === "script" && (
              <>
                <Labeled label="Script" help="The file the cluster actually runs, relative to the folder you picked." className="sm:col-span-2">
                  {scriptOptions.length > 0 ? (
                    <Select
                      ariaLabel="Entry-point script"
                      value={script}
                      onChange={setScript}
                      options={[
                        { value: "", label: "Choose a script…" },
                        ...scriptOptions.map((p) => ({ value: p, label: p })),
                      ]}
                    />
                  ) : (
                    <p className="field flex items-center text-[13px] text-ink-mute">
                      no .py files in the picked folder
                    </p>
                  )}
                </Labeled>
                <Labeled label="Output folders" help="Folders your results are written into. Anything written outside them is recoverable for 30 minutes after the Spawn ends and then gone, so name the right ones rather than relying on that. What lands in them is kept 7 days." className="sm:col-span-2">
                  <div className="flex flex-wrap items-center gap-2 rounded-[var(--radius-control)]">
                    {outputs.map((o) => (
                      <span
                        key={o}
                        className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] px-3 py-1.5 text-[13px]"
                        style={{ background: "color-mix(in oklab, var(--shoal) 45%, transparent)", color: "var(--ink)" }}
                      >
                        {o}
                        <button
                          type="button"
                          onClick={() => removeOutput(o)}
                          aria-label={`Remove output ${o}`}
                          className="text-ink-mute hover:text-ink"
                        >
                          ×
                        </button>
                      </span>
                    ))}
                    <input
                      className="field min-w-[140px] flex-1"
                      placeholder={outputs.length ? "another name…" : "checkpoints, outputs, etc..."}
                      value={outputDraft}
                      onChange={(e) => setOutputDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === ",") {
                          e.preventDefault();
                          addOutput();
                        } else if (e.key === "Backspace" && !outputDraft && outputs.length) {
                          removeOutput(outputs[outputs.length - 1]);
                        }
                      }}
                      onBlur={addOutput}
                    />
                  </div>
                </Labeled>
                <Labeled label="Arguments" help="Passed to your script exactly as typed, the same as if you had run it yourself." className="sm:col-span-2">
                  <input className="field" placeholder="optional" value={args} onChange={(e) => setArgs(e.target.value)} />
                </Labeled>
                {/* Blank is the normal answer — the Head looks in the project root for uv.lock,
                    pyproject.toml, requirements.txt or environment.yml. This field is for the two cases
                    it can't guess: a manifest that isn't the obvious one, and "don't build anything".
                    Grouped with Script/Arguments because it describes what the code IS, not what it needs. */}
                <Labeled label="Dependencies" help="Which file builds your Python environment. Blank means the Head looks in your project root for uv.lock, pyproject.toml, requirements.txt or environment.yml; 'none' means build nothing and run against the base environment." className="sm:col-span-2">
                  <input
                    className="field"
                    placeholder="auto-detect (or a path, or 'none')"
                    value={manifest}
                    onChange={(e) => setManifest(e.target.value)}
                  />
                </Labeled>
                {/* Client's half of the W&B environment (the key is the Head's); grouped with Name/Script/
                    Arguments, not the resource asks below. Nothing renders until status lands, so fields
                    never flicker in for a User who logs nowhere. Toggle only shows for a User who HAS a
                    key — a switch that turns nothing off is noise. Off HIDES project/entity rather than
                    dimming them, so "off" looks the same whether from no key or the toggle. */}
                {wandbOn === false ? (
                  <p className="text-[13px] text-ink-mute sm:col-span-2">
                    <Link to="/account" className="text-[var(--gold)] underline-offset-4 hover:underline">
                      Add a key for W&amp;B
                    </Link>
                  </p>
                ) : wandbOn === true ? (
                  <>
                    <div className="sm:col-span-2">
                      <span className="label mb-1.5 block">Use W&amp;B?</span>
                      <Tip
                        className="block"
                        focusable={false}
                        text="Whether this run logs to Weights &amp; Biases. Your stored key is used for every Spawn unless you turn it off here."
                      >
                        <div className="flex gap-1">
                          <Chip on={wandbLog} onClick={() => setWandbLog(true)}>Enabled</Chip>
                          <Chip on={!wandbLog} onClick={() => setWandbLog(false)}>Disabled</Chip>
                        </div>
                      </Tip>
                    </div>
                    {wandbLog && (
                      <>
                        <Labeled label="W&B project" help="Which W&B project this run logs into. Blank uses the cluster's default.">
                          <input
                            className="field"
                            placeholder="ccc"
                            value={wandbProject}
                            onChange={(e) => setWandbProject(e.target.value)}
                          />
                        </Labeled>
                        <Labeled label="W&B entity" help="The W&B team or account the run logs under. Blank uses whatever your key's account defaults to.">
                          <input
                            className="field"
                            placeholder="account default"
                            value={wandbEntity}
                            onChange={(e) => setWandbEntity(e.target.value)}
                          />
                        </Labeled>
                      </>
                    )}
                  </>
                ) : null}
              </>
            )}
            <Labeled label="Memory" help="How much GPU memory you need. Every card with at least this much becomes eligible, and the fastest free one takes the job — asking for more than you need narrows the pool and makes you wait longer.">
              <NumberField value={vram} onChange={setVram} min={1} placeholder="VRAM, GB" ariaLabel="Memory, VRAM in GB" />
            </Labeled>
            {/* A picker over what the Head supports, not free-text like Output folders above — the
                catalog is built into CCC, so anything outside it can never be satisfied. The guard is
                for a Head that failed to answer, not an empty catalog: there is no longer such a thing. */}
            {featureCatalog.length > 0 && (
              <Labeled label="Required features" help="Heavy or licence-gated software that isn't installed everywhere, like IsaacSim or VSim. Most runs need none. If no Arm in the fleet has what you ask for, the Head refuses the Spawn rather than queueing it forever." className="sm:col-span-2">
                <div className="flex flex-wrap gap-1.5">
                  {featureCatalog.map((f) => (
                    <Chip key={f} on={features.includes(f)} onClick={() => toggleFeature(f)}>
                      {f}
                    </Chip>
                  ))}
                </div>
              </Labeled>
            )}
            <Labeled label="Time" help="How long the run may take before it is killed. Karma is escrowed against it, so over-estimating costs you queue position and nothing else — the difference is refunded when the Spawn ends.">
              <NumberField value={minutes} onChange={setMinutes} min={1} placeholder="minutes" ariaLabel="Time in minutes" />
            </Labeled>
            <Labeled label="GPUs" help="How many separate cards this run holds at once. Each one is priced separately.">
              <NumberField value={gpu} onChange={(v) => setGpu(Number(v))} min={0} ariaLabel="GPUs" />
            </Labeled>
            {/* Naming a machine is an Admin power — a User says what they NEED and the scheduler places
                it. Non-admins never see this (and the Head 403s it). */}
            {isAdmin && (
              <Labeled label="Arm" help="Admin only. Everyone else declares what they need and the cluster places the work — naming a machine is how a queue stops being fair.">
                <Select
                  ariaLabel="Target an arm"
                  value={node}
                  onChange={setNode}
                  options={[{ value: "", label: "Any eligible" }, ...arms.map((a) => ({ value: a.name, label: a.name }))]}
                />
              </Labeled>
            )}
          </div>

          {error && <p className="text-[13px] text-[var(--alarm)]">{error}</p>}

          {/* Survives the form reset on purpose — it is about the Spawn that just went, and the form
              it was submitted from is already gone. */}
          {envNote && <p className="text-[13px] text-[var(--muted)]">{envNote}</p>}

          {/* Submit sits dead-centre; Clear hangs off its right without shifting it — the auto centre
              column is flanked by equal 1fr rails, so the primary verb stays centred whether or not
              Clear is shown. */}
          <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3">
            <span aria-hidden />
            <button className="btn-primary justify-self-center" onClick={go} disabled={!ready || busy}>
              {busy
                ? mode === "tidepool" ? "Requesting…" : "Submitting…"
                : mode === "tidepool" ? "Request tide pool" : "Submit spawn"}
            </button>
            {picked ? (
              <button className="btn-ghost justify-self-start" onClick={reset} disabled={busy}>
                Clear
              </button>
            ) : (
              <span aria-hidden />
            )}
          </div>
        </div>
      )}
    </section>
    </>
  );
}

/**
 * Shown once, right after a Tide Pool is placed in the queue — the one moment the attach command actually
 * matters, so it carries the real id instead of living in compose-time text nobody reads twice. The
 * Dashboard's part ends here: the shell itself is CLI-only (docs/TODO.md "Tide Pool"), no browser terminal.
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

function Labeled({ label, help, className, children }: { label: string; help?: string; className?: string; children: React.ReactNode }) {
  // Plain div, not a <label>: a <label> wrapping a non-native widget (Select) forwards a SECOND click
  // onto the toggle button whenever a menu item is clicked (the <li> is the label's descendant but not
  // the button's), reopening the menu the pick just closed.
  // Tip wraps the CONTROL, not the label above it — the pointer is already there, about to type, which
  // is when the explanation is wanted (see app/Tip.tsx: coverage is the affordance). `focusable={false}`:
  // the input inside brings its own tab stop; focusing it opens the panel anyway.
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
