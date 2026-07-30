import { useCallback, useRef, useState } from "react";
import { human, pick, type Picked } from "../project";
import { NumberField } from "../app/NumberField";
import { Chip } from "../app/Chip";
import { ALLOWANCE, size } from "./format";

/**
 * Freeze a project into a Recipe. Structurally the Spawn composer's twin — same drop-strip recess, same
 * pick/preview gate, bytes move only on Save — but it ends in "Save recipe", not "Submit spawn", and it
 * answers to the storage wall above it instead of the queue. Three things differ from a Spawn:
 *
 *   · Name is REQUIRED and unique — it is the Recipe's identity, the key you Run it by later.
 *   · No "Target an Arm" — a Recipe runs from ANY machine with nothing to hand; pinning a node contradicts
 *     the whole point.
 *   · The 20 GB wall is a hard gate. We know the folder's size from File.size at pick time (no upload), so
 *     a save that wouldn't fit is blocked HERE, before the upload — the wall shows the breach in --alarm and
 *     this copy names how far over. You delete a Recipe to make room, never dismiss a warning.
 *
 * The composer is standalone (the Queue composer stays untouched); the drop strip is copied, not shared.
 */

const GB = 1024 ** 3;

// The drop strip's soft-edged pool — a rounded recess feathered by a blur, behind crisp copy. Copied from
// the Spawn composer; see its note on why a blur (not the surface mask) is what makes the radius bite.
const DROP_FADE = { radius: 32, feather: 8 };

export type RecipePayload = {
  picked: Picked;
  name: string;
  script: string;
  args: string[];
  gpus: number;
  vram_mb: number;
  minutes: number;
  // Experiment-tracking config, frozen with the code — a Recipe holds it (CONTEXT.md) so a re-run
  // doesn't re-answer it. NOTHING PERSISTS IT YET: the Head serves no /recipes route at all, so these
  // are collected, sent, and dropped, exactly like every other field here. Asked now so the Recipe's
  // shape is already right when Phase 5 lands the backend.
  wandb: boolean;
  wandb_project: string;
  wandb_entity: string;
};

export function RecipeComposer({
  used,
  existingNames,
  onPending,
  onSave,
  onCancel,
}: {
  /** Bytes already committed to the wall — the fit test adds the picked folder to this. */
  used: number;
  /** Names already taken on this shelf; a Recipe's name is its key, so it must be unique. */
  existingNames: string[];
  /** Report the picked folder's bytes up so the page can preview them as a ghost on the wall. */
  onPending: (bytes: number) => void;
  onSave: (r: RecipePayload) => void;
  onCancel: () => void;
}) {
  const [picked, setPicked] = useState<Picked | null>(null);
  const [honor, setHonor] = useState(true);
  const [name, setName] = useState("");
  const [script, setScript] = useState("");
  const [gpu, setGpu] = useState(1);
  const [minutes, setMinutes] = useState("");
  const [vram, setVram] = useState("");
  const [args, setArgs] = useState("");
  const [wandbLog, setWandbLog] = useState(true);
  const [wandbProject, setWandbProject] = useState("");
  const [wandbEntity, setWandbEntity] = useState("");
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

  const trimmed = name.trim();
  const dup = existingNames.includes(trimmed);
  const projected = (picked?.bytes ?? 0) + used;
  const over = projected > ALLOWANCE;
  const overBy = projected - ALLOWANCE;
  const ready = !!picked && !!trimmed && !dup && !!script && Number(minutes) > 0 && Number(vram) > 0 && !over;

  const save = () => {
    if (!ready || !picked) return;
    onSave({
      picked,
      name: trimmed,
      script,
      args: args.split(/\s+/).filter(Boolean),
      gpus: gpu,
      vram_mb: Number(vram) * 1024,
      minutes: Number(minutes),
      wandb: wandbLog,
      wandb_project: wandbProject.trim(),
      wandb_entity: wandbEntity.trim(),
    });
  };

  return (
    <section className="surface [--sink:35%]" style={{ padding: "calc(var(--pad) + 50px) calc(var(--pad) + 50px)" }}>
      <h2 className="label mb-6">New recipe</h2>

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
              <span className="num text-ink">{picked.root}</span> — choose a different folder
            </>
          ) : (
            <>Drop a project to freeze, or choose a folder</>
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

        <div className="grid gap-3 sm:grid-cols-2">
          <Labeled label="Name" className="sm:col-span-2">
            <input className="field" placeholder="optional" value={name} onChange={(e) => setName(e.target.value)} />
            {dup && <span className="mt-1 block text-[12px]" style={{ color: "var(--alarm)" }}>You already have a recipe named {trimmed}.</span>}
          </Labeled>
          <Labeled label="Script" className="sm:col-span-2">
            <input className="field" placeholder="train.py" value={script} onChange={(e) => setScript(e.target.value)} />
          </Labeled>
          <Labeled label="Arguments" className="sm:col-span-2">
            <input className="field" placeholder="optional" value={args} onChange={(e) => setArgs(e.target.value)} />
          </Labeled>
          {/* Experiment tracking, asked once and frozen with the code rather than on every run — see
              RecipePayload for why it lands nowhere yet.

              Deliberately NOT gated on whether a key is stored, unlike the Spawn composer. A Spawn runs
              now, so the now-state is exactly the right thing to gate it on; a Recipe is durable config
              that outlives it. Hiding these because the User has no key today would freeze a Recipe with
              no project for a reason that stops being true the moment they paste one. Off still hides
              project/entity — that is the User's own answer, not a fact about their account. */}
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
          <Labeled label="Memory">
            <NumberField value={vram} onChange={setVram} min={1} placeholder="VRAM, GB" ariaLabel="Memory, VRAM in GB" />
          </Labeled>
          <Labeled label="Time">
            <NumberField value={minutes} onChange={setMinutes} min={1} placeholder="minutes" ariaLabel="Time in minutes" />
          </Labeled>
          <Labeled label="GPUs">
            <NumberField value={gpu} onChange={(v) => setGpu(Number(v))} min={0} ariaLabel="GPUs" />
          </Labeled>
        </div>

        {/* Save sits dead-centre; Cancel hangs off its right without shifting it. */}
        <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3">
          <span aria-hidden />
          <button className="btn-primary justify-self-center" onClick={save} disabled={!ready}>
            Save recipe
          </button>
          <button className="btn-ghost justify-self-start" onClick={cancel}>
            Cancel
          </button>
        </div>
      </div>
    </section>
  );
}

function Labeled({ label, className, children }: { label: string; className?: string; children: React.ReactNode }) {
  return (
    <label className={`block ${className ?? ""}`}>
      <span className="label mb-1.5 block">{label}</span>
      {children}
    </label>
  );
}
