import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Arm } from "../api";
import { getFeatureCatalog, getWandbKey, submit, submitTidepool } from "../api";
import { human, pick, relPath, type Picked } from "../project";
import { Select } from "../app/Select";
import { NumberField } from "../app/NumberField";
import { Chip } from "../app/Chip";
import { Link } from "../app/router";

// Mirrors the Head's OUTPUT_NAME_RE (ccc_head_api.spawns) — an output name becomes a shell-interpolated
// `mkdir -p "$ccc_out/<name>"`, so the same "no slashes, no leading dot" rule is enforced here too. A
// client-side reject is instant; the Head's is the one that actually matters (it never trusts this one).
const OUTPUT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

/**
 * Submit a Spawn. The affordance IS the form: an empty strip that says "drop a project or choose a
 * folder", and picking one expands the composer inline. That expansion is also the PREVIEW GATE —
 * name, file count, total size — and nothing uploads until Submit. Bytes move on one click, never on
 * the pick.
 *
 * Source is folder today; a Recipe is the other way in (Ph5) and will drop into the same composer.
 * That's why the picked project is held behind `source`, not assumed to be a folder everywhere.
 *
 * Two MODES (docs/TODO.md, "Tide Pool"): "Script" is everything above — a project, an entry point,
 * declared outputs. "Tide Pool" is a live interactive shell instead: it hides the project/folder-upload
 * story entirely (nothing to pick, nothing to preview, nothing to upload) and submits only the declared
 * needs (time/vram/gpu/features) any Spawn declares. The Dashboard's part ends there — actual shell
 * access is CLI-only (`cuttle run --tidepool <id>`); this just requests one and shows it in the queue
 * like any other Spawn once placed.
 */

const GB = 1024 ** 3;

/*
 * The drop strip's soft-edged pool. A ROUNDED RECT (border-radius) feathered by a blur, on a layer BEHIND
 * the label text so the fill melts at the rim while the copy stays crisp.
 *
 * NOT the surfaces' crossed-linear-ramp mask here: that mask feathers SQUARE corners, and its ~2·fade px
 * dead-zone at every edge swallowed the rounded corner whole until the radius grew past it — so raising
 * `radius` did nothing visible. A blur feathers the true rounded-rect perimeter, so `radius` controls the
 * roundness at every value. Edit, save, watch.
 */
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
  // Arm Features (docs/TODO.md) this Spawn requires — a picker over the Head's catalog, never
  // free-typed (unlike outputs above): a name the Admin never curated can't be a real requirement.
  // Starts empty and stays optional; the Head 422s only an UNSATISFIABLE request, never an empty one.
  const [featureCatalog, setFeatureCatalog] = useState<string[]>([]);
  const [features, setFeatures] = useState<string[]>([]);
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
      .catch(() => setWandbOn(true)); // includes ?demo, where the route is behind a session we don't have
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
        await submit({
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
        });
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
        <Chip on={mode === "tidepool"} onClick={() => setMode("tidepool")}>Tide Pool</Chip>
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
              <>Drop/choose a folder — optional</>
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
            <Labeled label="Name" className="sm:col-span-2">
              <input className="field" placeholder="optional" value={name} onChange={(e) => setName(e.target.value)} />
            </Labeled>
            {mode === "script" && (
              <>
                <Labeled label="Script" className="sm:col-span-2">
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
                <Labeled label="Output folders" className="sm:col-span-2">
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
                <Labeled label="Arguments" className={isAdmin ? "sm:col-span-2" : "sm:col-span-2"}>
                  <input className="field" placeholder="optional" value={args} onChange={(e) => setArgs(e.target.value)} />
                </Labeled>
                {/* Which W&B run this is — the client's half of the environment; the key is the Head's.
                    Grouped with Name/Script/Arguments (what the run IS) rather than with the resource asks
                    below. Nothing renders until the status lands, so the fields never flicker in for a User
                    whose Spawns log nowhere.
                    The toggle is per-Spawn and only ever shown to a User who HAS a key — without one the
                    line below already says W&B is off, and a switch that turns nothing off is noise.
                    Switching it off HIDES project/entity rather than dimming them: a keyless User sees no
                    fields either, so "off" looks the same however you got there. */}
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
                      <div className="flex gap-1">
                        <Chip on={wandbLog} onClick={() => setWandbLog(true)}>Enabled</Chip>
                        <Chip on={!wandbLog} onClick={() => setWandbLog(false)}>Disabled</Chip>
                      </div>
                    </div>
                    {wandbLog && (
                      <>
                        <Labeled label="W&B project">
                          <input
                            className="field"
                            placeholder="ccc"
                            value={wandbProject}
                            onChange={(e) => setWandbProject(e.target.value)}
                          />
                        </Labeled>
                        <Labeled label="W&B entity">
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
            <Labeled label="Memory">
              <NumberField value={vram} onChange={setVram} min={1} placeholder="VRAM, GB" ariaLabel="Memory, VRAM in GB" />
            </Labeled>
            {/* A picker over the Head's catalog, not free-text like Output folders above — a required
                Feature has to be a name the Admin actually curated (`cuttle feature add`), or it can
                never be satisfied. Omitted entirely when the catalog is empty: nothing to require yet. */}
            {featureCatalog.length > 0 && (
              <Labeled label="Required features" className="sm:col-span-2">
                <div className="flex flex-wrap gap-1.5">
                  {featureCatalog.map((f) => (
                    <Chip key={f} on={features.includes(f)} onClick={() => toggleFeature(f)}>
                      {f}
                    </Chip>
                  ))}
                </div>
              </Labeled>
            )}
            <Labeled label="Time">
              <NumberField value={minutes} onChange={setMinutes} min={1} placeholder="minutes" ariaLabel="Time in minutes" />
            </Labeled>
            <Labeled label="GPUs">
              <NumberField value={gpu} onChange={(v) => setGpu(Number(v))} min={0} ariaLabel="GPUs" />
            </Labeled>
            {/* Naming a machine is an Admin power — a User says what they NEED and the scheduler places
                it. Non-admins never see this (and the Head 403s it). */}
            {isAdmin && (
              <Labeled label="Arm">
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
 * Shown once, right after a Tide Pool is placed in the queue — this is the one moment the attach command
 * actually matters, so it carries the real id rather than living in compose-time text nobody reads twice.
 * The Dashboard's part in a Tide Pool ends here: it can request one and show it in the queue, but the
 * shell itself is CLI-only (docs/TODO.md "Tide Pool") — there is no browser terminal to open instead.
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

function Labeled({ label, className, children }: { label: string; className?: string; children: React.ReactNode }) {
  // A plain div, not a <label> — a <label> wrapping a non-native widget (Select below) forwards a
  // SECOND click onto the labeled control (the toggle button) whenever you click one of its menu items,
  // since the <li> is the label's descendant but not the button's. That reopens the menu the pick just
  // closed. A native <input>/<NumberField> loses nothing here; clicking the text above them never
  // focused them either way once this stopped being a real <label>.
  return (
    <div className={`block ${className ?? ""}`}>
      <span className="label mb-1.5 block">{label}</span>
      {children}
    </div>
  );
}
