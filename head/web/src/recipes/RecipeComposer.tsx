import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { human, pick, relPath, type Picked } from "../project";
import type { Recipe } from "../api";
import { NumberField } from "../app/NumberField";
import { Chip } from "../app/Chip";
import { Select } from "../app/Select";
import { Tip } from "../app/Tip";
import { ALLOWANCE, size } from "./format";

/**
 * Freeze a project into a Recipe. Structurally the Spawn composer's twin — same drop-strip recess, same
 * pick/preview gate, bytes move only on Save — but ends in "Save recipe" and answers to the storage wall,
 * not the queue. Three things differ from a Spawn:
 *
 *   · Name is REQUIRED and unique — the Recipe's identity, the key you Run it by later.
 *   · No "Target an Arm" — a Recipe runs from ANY machine with nothing to hand; pinning a node contradicts
 *     the point.
 *   · The 24 GB wall is a hard gate, checked from File.size at pick time (no upload): a save that wouldn't
 *     fit is blocked HERE. You delete a Recipe to make room, never dismiss a warning.
 *
 * IS a grid tile: root is the `<li>` itself, row-spanning 2 so it grows in whichever cell it took over
 * rather than replacing the whole shelf. That cell is one column wide, so fields below stack single-column
 * always, unlike the Spawn composer's two-up layout.
 */

const GB = 1024 ** 3;

// Mirrors the Head's OUTPUT_NAME_RE (ccc_head_api.spawns), same as the Spawn composer's copy. Frozen
// into the Recipe rather than asked at run time: a Recipe with no declared outputs is one `/submit`
// would refuse, and finding that out the day you need it is worse than the day you saved it.
const OUTPUT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

// The drop strip's soft-edged pool — a rounded recess feathered by a blur, behind crisp copy. Copied from
// the Spawn composer; see its note on why a blur (not the surface mask) is what makes the radius bite.
const DROP_FADE = { radius: 32, feather: 8 };

export type RecipePayload = {
  picked: Picked;
  name: string;
  script: string;
  args: string[];
  outputs: string[];
  gpus: number;
  vram_mb: number;
  minutes: number;
  // Experiment-tracking config, frozen with the code — a Recipe holds it (CONTEXT.md) so a re-run
  // doesn't re-answer it.
  wandb: boolean;
  wandb_project: string;
  wandb_entity: string;
};

export function RecipeComposer({
  used,
  existingNames,
  editing,
  onPending,
  onSave,
  onCancel,
}: {
  /**
   * Bytes already committed to the wall — the fit test adds the picked folder to this. When `editing`,
   * the page has ALREADY subtracted that Recipe's own bytes: an update is delete-then-save on the Head,
   * charged what it grows by, and a preview that showed the peak would refuse saves the Head accepts.
   */
  used: number;
  /** Names already taken on this shelf; a Recipe's name is its key, so it must be unique. */
  existingNames: string[];
  /**
   * The Recipe being re-captured, or undefined for a new one. Its settings prefill the form and its name
   * is fixed — an update replaces a snapshot in place, and renaming through it would silently leave the
   * old name behind holding bytes nobody meant to keep.
   */
  editing?: Recipe;
  /** Report the picked folder's bytes up so the page can preview them as a ghost on the wall. */
  onPending: (bytes: number) => void;
  onSave: (r: RecipePayload) => void;
  onCancel: () => void;
}) {
  const [picked, setPicked] = useState<Picked | null>(null);
  const [honor, setHonor] = useState(true);
  const [name, setName] = useState(editing?.name ?? "");
  const [script, setScript] = useState(editing?.script ?? "");
  const [gpu, setGpu] = useState(editing?.gpus ?? 1);
  const [minutes, setMinutes] = useState(editing ? String(editing.minutes) : "");
  const [vram, setVram] = useState(editing ? String(editing.vram_mb / 1024) : "");
  const [args, setArgs] = useState(editing?.args.join(" ") ?? "");
  const [outputs, setOutputs] = useState<string[]>(editing?.outputs ?? []);
  const [outputDraft, setOutputDraft] = useState("");
  const [outputError, setOutputError] = useState<string | null>(null);
  const [wandbLog, setWandbLog] = useState(editing?.wandb ?? true);
  const [wandbProject, setWandbProject] = useState(editing?.wandb_project ?? "");
  const [wandbEntity, setWandbEntity] = useState(editing?.wandb_entity ?? "");
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const lastList = useRef<FileList | null>(null);

  const setPick = useCallback(
    (p: Picked | null) => {
      setPicked(p);
      onPending(p?.bytes ?? 0);
    },
    [onPending],
  );

  // The script picker's choices: every .py in the picked tree, so "script must exist" is true by
  // construction — same as the Spawn composer. `editing`'s script prefills `script` before any folder is
  // (re-)picked, so it survives here until a pick either confirms it (still present) or the effect below
  // clears it (renamed/removed in the new folder).
  const scriptOptions = useMemo(
    () => (picked ? picked.files.filter((f) => f.name.endsWith(".py")).map(relPath).sort() : []),
    [picked],
  );

  useEffect(() => {
    if (script && !scriptOptions.includes(script) && picked) setScript("");
  }, [scriptOptions, script, picked]);

  const rescan = useCallback(
    async (list: FileList | null, honorGitignore: boolean) => {
      if (!list || list.length === 0) return;
      setPick(await pick(list, honorGitignore));
    },
    [setPick],
  );

  const onPickFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
    lastList.current = e.target.files;
    await rescan(e.target.files, honor);
  };

  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files?.length) {
      lastList.current = e.dataTransfer.files;
      await rescan(e.dataTransfer.files, honor);
    }
  };

  const toggleHonor = async (v: boolean) => {
    setHonor(v);
    await rescan(lastList.current, v);
  };

  const cancel = () => {
    onPending(0);
    onCancel();
  };

  const addOutput = () => {
    const out = outputDraft.trim();
    if (!out) return;
    if (!OUTPUT_NAME_RE.test(out)) {
      setOutputError(`"${out}" isn't a valid output name — letters, digits, '.', '_', '-' only, and it can't start with '.'`);
      return;
    }
    setOutputError(null);
    setOutputs((prev) => (prev.includes(out) ? prev : [...prev, out]));
    setOutputDraft("");
  };

  const removeOutput = (out: string) => setOutputs((prev) => prev.filter((o) => o !== out));

  const trimmed = name.trim();
  const dup = existingNames.includes(trimmed);
  const projected = (picked?.bytes ?? 0) + used;
  const over = projected > ALLOWANCE;
  const overBy = projected - ALLOWANCE;
  const ready =
    !!picked && !!trimmed && !dup && !!script && Number(minutes) > 0 && Number(vram) > 0 &&
    outputs.length > 0 && !over;

  const save = () => {
    if (!ready || !picked) return;
    onSave({
      picked,
      name: trimmed,
      script,
      args: args.split(/\s+/).filter(Boolean),
      outputs,
      gpus: gpu,
      vram_mb: Number(vram) * 1024,
      minutes: Number(minutes),
      wandb: wandbLog,
      wandb_project: wandbProject.trim(),
      wandb_entity: wandbEntity.trim(),
    });
  };

  return (
    <li className="shard [--sink:35%] row-span-2" style={{ padding: "1.9rem 1.9rem" }}>
      <h2 className="label mb-6">{editing ? `Update ${editing.name}` : "New recipe"}</h2>

      {/* An update replaces the snapshot, so say what is about to stop being true. Until Save lands, the
          Recipe keeps running the old code — and a save the wall refuses leaves it doing exactly that. */}
      {editing && (
        <p className="mb-6 px-1 text-[13px] text-ink-mute">
          Re-freezing this recipe around a new folder. It keeps running the old snapshot until this
          lands, and its <span className="num">{size(editing.size_bytes)}</span> comes off the wall when
          it does.
        </p>
      )}

      {/* The strip — a recess you drop a folder onto, or click to choose one. */}
      <label
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className="relative flex cursor-pointer items-center justify-center px-6 py-7 text-center"
        style={{ borderRadius: DROP_FADE.radius, minHeight: DROP_FADE.radius * 2 }}
      >
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
          onChange={onPickFiles}
          className="sr-only"
          {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
        />
        <span className="relative text-[15px] text-ink-soft">
          {picked ? (
            <>
              <span className="num text-ink">{picked.root}</span>
            </>
          ) : (
            <>Drop/choose a folder</>
          )}
        </span>
      </label>

      <div className="mt-6 space-y-5">
        {/* The preview gate — what the pick told us, and the promise that nothing has moved yet. */}
        {picked && (
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 px-1 text-[13px]">
            <span className="text-ink-soft">
              <span className="num text-ink">{picked.files.length}</span> of{" "}
              <span className="num">{picked.scanned}</span> files,{" "}
              <span className="num text-ink">{human(picked.bytes)}</span> — holds against your allowance
            </span>
            <label className="ml-auto flex items-center gap-2 text-ink-mute">
              <input
                type="checkbox"
                checked={honor}
                onChange={(e) => toggleHonor(e.target.checked)}
                className="accent-[var(--gold)]"
              />
              honor .gitignore
            </label>
          </div>
        )}

        {/* The hard wall. Blocked before the upload — you delete a Recipe to make room, never dismiss this. */}
        {over && (
          <p className="rounded-[10px] px-3 py-2 text-[13px]" style={{ background: "color-mix(in oklab, var(--alarm) 12%, transparent)", color: "var(--ink-soft)" }}>
            <span className="num" style={{ color: "var(--alarm)" }}>{size(overBy)}</span> over your{" "}
            <span className="num">{size(ALLOWANCE)}</span> — delete a recipe to make room.
          </p>
        )}

        {picked && !over && picked.bytes > GB && (
          <p className="rounded-[10px] px-3 py-2 text-[13px]" style={{ background: "color-mix(in oklab, var(--irid-low) 10%, transparent)", color: "var(--ink-soft)" }}>
            That's over a gigabyte held on the Head for good. Check your .gitignore excludes data and
            checkpoints.
          </p>
        )}

        <div className="grid gap-3">
          <Labeled label="Name" help="What you'll call this Recipe when you run it later. It has to be unique among yours.">
            {/* Fixed while editing: an update replaces a snapshot in place, and letting the name move
                through it would leave the old one behind holding bytes nobody meant to keep. */}
            <input
              className="field"
              placeholder="optional"
              value={name}
              readOnly={!!editing}
              onChange={(e) => setName(e.target.value)}
            />
            {dup && <span className="mt-1 block text-[12px]" style={{ color: "var(--alarm)" }}>You already have a recipe named {trimmed}.</span>}
          </Labeled>
          <Labeled label="Script" help="The file the cluster actually runs, relative to the folder you picked.">
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
            ) : !picked && editing ? (
              // Nothing picked yet, but there's a live snapshot's script to show — the dropdown has
              // nothing to offer until a folder lands, so name what's still running instead of going blank.
              <p className="field flex items-center text-[13px] text-ink-mute">
                <span className="num text-ink-soft">{script}</span>&nbsp;— pick a folder to change it
              </p>
            ) : (
              <p className="field flex items-center text-[13px] text-ink-mute">
                no .py files in the picked folder
              </p>
            )}
          </Labeled>
          <Labeled label="Arguments" help="Passed to your script exactly as typed, the same as if you had run it yourself.">
            <input className="field" placeholder="optional" value={args} onChange={(e) => setArgs(e.target.value)} />
          </Labeled>
          {/* Declared outputs, frozen with the code — the same chips the Spawn composer uses, asked once
              here instead of on every run. Required: a Recipe with none is one the Head would refuse to
              run, and that is far better discovered now than the day you reach for it. */}
          <Labeled label="Output folders" help="Folders your results are written into. Anything written outside them is recoverable for 30 minutes after the Spawn ends and then gone, so name the right ones rather than relying on that. What lands in them is kept 7 days.">
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
            {outputError && (
              <span className="mt-1 block text-[12px]" style={{ color: "var(--alarm)" }}>{outputError}</span>
            )}
          </Labeled>
          {/* Experiment tracking, asked once and frozen with the code rather than on every run — see
              RecipePayload for why it lands nowhere yet.

              Deliberately NOT gated on whether a key is stored, unlike the Spawn composer. A Spawn runs
              now, so the now-state is exactly the right thing to gate it on; a Recipe is durable config
              that outlives it. Hiding these because the User has no key today would freeze a Recipe with
              no project for a reason that stops being true the moment they paste one. Off still hides
              project/entity — that is the User's own answer, not a fact about their account. */}
          <div>
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
          <Labeled label="Memory" help="How much GPU memory you need. Every card with at least this much becomes eligible, and the fastest free one takes the job — asking for more than you need narrows the pool and makes you wait longer.">
            <NumberField value={vram} onChange={setVram} min={1} placeholder="VRAM, GB" ariaLabel="Memory, VRAM in GB" />
          </Labeled>
          <Labeled label="Time" help="How long the run may take before it is killed. Karma is escrowed against it, so over-estimating costs you queue position and nothing else — the difference is refunded when the Spawn ends.">
            <NumberField value={minutes} onChange={setMinutes} min={1} placeholder="minutes" ariaLabel="Time in minutes" />
          </Labeled>
          <Labeled label="GPUs" help="How many separate cards this run holds at once. Each one is priced separately.">
            <NumberField value={gpu} onChange={(v) => setGpu(Number(v))} min={0} ariaLabel="GPUs" />
          </Labeled>
        </div>

        {/* Save sits dead-centre; Cancel hangs off its right without shifting it. */}
        <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3">
          <span aria-hidden />
          <button className="btn-primary justify-self-center" onClick={save} disabled={!ready}>
            {editing ? "Update code" : "Save recipe"}
          </button>
          <button className="btn-ghost justify-self-start" onClick={cancel}>
            Cancel
          </button>
        </div>
      </div>
    </li>
  );
}

function Labeled({ label, help, className, children }: { label: string; help?: string; className?: string; children: React.ReactNode }) {
  // The CONTROL is what you point at, not the label — same as the Spawn composer, and the strings are
  // the same too: a Recipe asks the same questions, and the answers do not differ by being frozen.
  return (
    <label className={`block ${className ?? ""}`}>
      <span className="label mb-1.5 block">{label}</span>
      {help ? (
        <Tip text={help} className="block" focusable={false}>{children}</Tip>
      ) : (
        children
      )}
    </label>
  );
}
