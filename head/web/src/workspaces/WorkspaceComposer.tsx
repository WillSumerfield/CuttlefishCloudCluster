import { useEffect, useRef, useState } from "react";
import { getFeatureCatalog, type Arm, type Workspace } from "../api";
import { Chip } from "../app/Chip";
import { Labeled } from "../app/Composer";
import { NumberField } from "../app/NumberField";
import { Select } from "../app/Select";
import { human, pick, type Picked } from "../project";
import { ALLOWANCE, GB, size } from "./format";

/**
 * Make a folder into a Workspace, or change what an existing one asks for.
 *
 * **Its own component, rather than a third `variant` of app/Composer.** That one is the Spawn/Recipe
 * pair, which ask nearly the same questions; a Workspace asks a different set. There is no script and no
 * arguments (a visit is a shell, not a run), no W&B (nothing is submitted here), the manifest and the
 * outputs are both optional the way a Tide Pool's are — and there is one question neither of those has
 * at all, which is how much room to reserve.
 *
 * **Storage is the field that matters, and it is asked once.** It comes out of a 32 GB pool the moment
 * the Workspace exists, so the fit test here is against the pool rather than against the folder: an
 * empty 16 GB Workspace costs its owner 16 GB. Editing can move it, but never below what the tree
 * already holds — that refusal is the Head's, with the real number in it.
 *
 * The folder is asked for ONCE, at creation. Editing offers no dropper at all: a Workspace is written
 * to from inside its own sessions, and there is nothing here that could re-seed it without destroying
 * what those sessions left.
 */
export type WorkspacePayload = {
  picked: Picked;
  name: string;
  storage_bytes: number;
  gpus: number;
  vram_mb: number;
  minutes: number;
  manifest: string;
  outputs: string[];
  features: string[];
};

export type WorkspaceSettingsPayload = Omit<WorkspacePayload, "picked" | "name">;

const DROP = { radius: 16, feather: 8, padX: 16, padY: 16 };

export function WorkspaceComposer({
  arms,
  reserved,
  existingNames,
  editing,
  onPending,
  onSave,
  onSaveMeta,
  onCancel,
}: {
  /** The fleet, only so the GPU control can grey out counts no Arm can host. */
  arms: Arm[];
  /** Bytes already reserved across this User's other Workspaces — the pool's committed side. */
  reserved: number;
  existingNames: string[];
  editing?: Workspace;
  /** Report the Storage being reserved so the meter can ghost it. */
  onPending: (bytes: number) => void;
  onSave: (w: WorkspacePayload) => void;
  onSaveMeta?: (w: WorkspaceSettingsPayload) => void;
  onCancel: () => void;
}) {
  const metaOnly = !!editing;

  const [picked, setPicked] = useState<Picked | null>(null);
  const [honor, setHonor] = useState(true);
  const [dragging, setDragging] = useState(false);
  const [name, setName] = useState(editing?.name ?? "");
  const [storageGb, setStorageGb] = useState(
    editing ? String(Math.max(1, Math.round(editing.storage_bytes / GB))) : "");
  const [gpu, setGpu] = useState(Math.max(1, editing?.gpus ?? 1));
  const [minutes, setMinutes] = useState(editing ? String(editing.minutes) : "");
  const [vram, setVram] = useState(editing ? String(editing.vram_mb / 1024) : "");
  const [manifest, setManifest] = useState(editing?.manifest ?? "");
  const [outputs, setOutputs] = useState<string[]>(editing?.outputs ?? []);
  const [outputDraft, setOutputDraft] = useState("");
  const [featureCatalog, setFeatureCatalog] = useState<string[]>([]);
  const [features, setFeatures] = useState<string[]>(editing?.features ?? []);
  const fileRef = useRef<HTMLInputElement>(null);
  const lastList = useRef<FileList | null>(null);

  useEffect(() => {
    getFeatureCatalog().then(setFeatureCatalog).catch(() => setFeatureCatalog([]));
  }, []);

  const storage = Number(storageGb) * GB;
  useEffect(() => onPending(storage > 0 ? storage : 0), [storage, onPending]);

  async function take(list: FileList | null, honorGitignore: boolean) {
    if (!list || !list.length) return;
    lastList.current = list;
    setPicked(await pick(list, honorGitignore));
  }

  const trimmed = name.trim();
  const dup = !metaOnly && existingNames.includes(trimmed);
  // Against the POOL, not against the folder: a Storage is spent the moment it is reserved. `reserved`
  // already excludes this Workspace's own bytes when editing, so a resize is weighed on what it grows by.
  const overPool = storage > 0 && reserved + storage > ALLOWANCE;
  const overBy = reserved + storage - ALLOWANCE;
  // The one fit test the folder itself has to pass: a seed bigger than the Storage it is landing in.
  const seedTooBig = !!picked && storage > 0 && picked.bytes > storage;
  // Shrinking below what the tree holds is the Head's refusal, not ours — but saying so here saves a
  // round trip on the one edit most likely to be wrong.
  const belowHeld = metaOnly && storage > 0 && storage < editing!.used_bytes;

  const ready =
    !!trimmed && !dup && !overPool && !seedTooBig && !belowHeld &&
    Number(storageGb) >= 1 && Number(vram) >= 1 && Number(minutes) >= 1 &&
    (metaOnly || !!picked);

  const gpuOptions = (() => {
    const most = arms.reduce((n, a) => Math.max(n, a.gpus ?? 0), 0);
    return Array.from({ length: Math.max(1, most) }, (_, i) => ({
      value: String(i + 1),
      label: String(i + 1),
    }));
  })();

  function addOutput() {
    const v = outputDraft.trim().replace(/,$/, "");
    if (v && !outputs.includes(v)) setOutputs([...outputs, v]);
    setOutputDraft("");
  }

  function save() {
    if (!ready) return;
    const common = {
      storage_bytes: storage,
      gpus: gpu,
      vram_mb: Number(vram) * 1024,
      minutes: Number(minutes),
      manifest: manifest.trim(),
      outputs,
      features,
    };
    if (metaOnly) onSaveMeta?.(common);
    else onSave({ picked: picked!, name: trimmed, ...common });
  }

  return (
    <div
      className="shard [--sink:35%] h-full"
      style={{
        padding: "64px 64px",
        "--radius-surface": "22px",
        "--fade-x": "26px",
        "--fade-y": "26px",
      } as React.CSSProperties}
    >
      <h2 className="label mb-6">{metaOnly ? `Edit ${editing!.name}` : "New workspace"}</h2>

      {metaOnly ? (
        <p className="mb-6 px-1 text-[13px] text-ink-mute">
          Settings only — a workspace is written to from inside its own sessions, so there is no folder
          to re-drop here. It currently holds <span className="num">{size(editing!.used_bytes)}</span>.
          {editing!.live ? " A visit is live; these take effect on the next one." : ""}
        </p>
      ) : (
        <label
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); take(e.dataTransfer.files, honor); }}
          className="relative flex cursor-pointer items-center justify-center text-center"
          style={{
            borderRadius: DROP.radius,
            minHeight: DROP.radius * 2,
            marginInline: DROP.padX,
            padding: `${DROP.padY}px 0`,
          }}
        >
          <span
            aria-hidden
            className="absolute inset-0 transition-[background] duration-200"
            style={{
              borderRadius: DROP.radius,
              background: dragging
                ? "color-mix(in oklab, var(--irid-low) 14%, var(--well))"
                : "color-mix(in oklab, var(--well) 70%, black)",
              filter: `blur(${DROP.feather}px)`,
            }}
          />
          <input
            ref={fileRef}
            type="file"
            onChange={(e) => take(e.target.files, honor)}
            className="sr-only"
            {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
          />
          <span className={`relative text-[15px] ${picked ? "text-ink-soft" : "text-ink-mute"}`}>
            {picked ? <span className="num text-ink">{picked.root}</span> : <>Drop/choose a folder to start it from</>}
          </span>
        </label>
      )}

      <div className="mt-6 space-y-5">
        {picked && (
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 px-1 text-[13px]">
            <span className="text-ink-soft">
              <span className="num text-ink">{picked.files.length}</span> of{" "}
              <span className="num">{picked.scanned}</span> files,{" "}
              <span className="num text-ink">{human(picked.bytes)}</span> — the only upload this ever takes
            </span>
            <label className="ml-auto flex items-center gap-2 text-ink-mute">
              <input
                type="checkbox"
                checked={honor}
                onChange={(e) => { setHonor(e.target.checked); take(lastList.current, e.target.checked); }}
                className="accent-[var(--gold)]"
              />
              honor .gitignore
            </label>
          </div>
        )}

        {overPool && (
          <p className="rounded-[10px] px-3 py-2 text-[13px]"
             style={{ background: "color-mix(in oklab, var(--alarm) 12%, transparent)", color: "var(--ink-soft)" }}>
            <span className="num" style={{ color: "var(--alarm)" }}>{size(overBy)}</span> over your{" "}
            <span className="num">{size(ALLOWANCE)}</span> — ask for a smaller storage, or delete a workspace.
          </p>
        )}
        {seedTooBig && (
          <p className="rounded-[10px] px-3 py-2 text-[13px]"
             style={{ background: "color-mix(in oklab, var(--alarm) 12%, transparent)", color: "var(--ink-soft)" }}>
            That folder is <span className="num">{human(picked!.bytes)}</span> and would not fit the storage
            you asked for. Reserve more, or start it from less.
          </p>
        )}
        {belowHeld && (
          <p className="rounded-[10px] px-3 py-2 text-[13px]"
             style={{ background: "color-mix(in oklab, var(--alarm) 12%, transparent)", color: "var(--ink-soft)" }}>
            It already holds <span className="num">{size(editing!.used_bytes)}</span>. A workspace is never
            shrunk below its own tree — open it and free space first.
          </p>
        )}

        <div className="grid gap-3">
          <Labeled label="Name" help="What you'll call this workspace when you open it later. It has to be unique among yours, and it cannot be changed afterwards.">
            <input
              className="field"
              placeholder="scratch"
              value={name}
              readOnly={metaOnly}
              onChange={(e) => setName(e.target.value)}
            />
            {dup && (
              <span className="mt-1 block text-[12px]" style={{ color: "var(--alarm)" }}>
                You already have a workspace called that.
              </span>
            )}
          </Labeled>

          <Labeled label="Storage" help="How much this workspace may hold, in GB, taken out of your 32 GB allowance the moment you create it — reserved whether or not you fill it. This is the one allowance you divide up yourself: a session that fills its storage stops being saved and says so, rather than eating into anybody else's room.">
            <NumberField value={storageGb} onChange={setStorageGb} min={1} placeholder="GB" ariaLabel="Storage in GB" />
            {Number(storageGb) >= 1 && !overPool && (
              <span className="mt-1 block text-[12px] text-[var(--muted)]">
                {size(Math.max(0, ALLOWANCE - reserved - storage))} of your allowance left after this
              </span>
            )}
          </Labeled>

          <Labeled label="Outputs" help="Folders in the workspace tree a visit writes results to. Optional here, unlike a Spawn's — everything in the tree is kept either way. Naming one means the end of each session also sends it home, to be downloaded like any Spawn's results.">
            <div className="flex flex-wrap items-center gap-1.5">
              {outputs.map((o) => (
                <span
                  key={o}
                  className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] px-3 py-1.5 text-[13px]"
                  style={{ background: "color-mix(in oklab, var(--shoal) 45%, transparent)", color: "var(--ink)" }}
                >
                  {o}
                  <button type="button" onClick={() => setOutputs(outputs.filter((x) => x !== o))}
                          aria-label={`Remove output ${o}`} className="text-ink-mute hover:text-ink">
                    ×
                  </button>
                </span>
              ))}
              <input
                className="field min-w-[140px] flex-1"
                placeholder={outputs.length ? "another name…" : "optional — checkpoints, results…"}
                value={outputDraft}
                onChange={(e) => setOutputDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addOutput(); }
                  else if (e.key === "Backspace" && !outputDraft && outputs.length) {
                    setOutputs(outputs.slice(0, -1));
                  }
                }}
                onBlur={addOutput}
              />
            </div>
          </Labeled>

          <Labeled label="Dependencies" help="Which file builds the Python environment a visit gets, relative to the workspace root. Optional, like a Tide Pool's — a workspace opened to poke at a box declares none. Name one and every visit starts in the environment your Spawn would have run in.">
            <input
              className="field"
              placeholder="optional — pyproject.toml, requirements.txt…"
              value={manifest}
              onChange={(e) => setManifest(e.target.value)}
            />
          </Labeled>

          {featureCatalog.length > 0 && (
            <Labeled label="Required features" help="Heavy or licence-gated software that isn't installed everywhere. Most workspaces need none. If no Arm has what you ask for, the Head refuses the visit rather than queueing it forever.">
              <div className="flex flex-wrap gap-1.5">
                {featureCatalog.map((f) => (
                  <Chip key={f} on={features.includes(f)}
                        onClick={() => setFeatures(features.includes(f) ? features.filter((x) => x !== f) : [...features, f])}>
                    {f}
                  </Chip>
                ))}
              </div>
            </Labeled>
          )}

          <Labeled label="Memory" help="TOTAL GPU memory a visit needs, across the cards it asks for. Asking for more than you need narrows the pool of eligible cards and makes you wait longer to get in.">
            <NumberField value={vram} onChange={setVram} min={1} placeholder="VRAM, GB" ariaLabel="Memory, VRAM in GB" />
          </Labeled>
          <Labeled label="Time" help="How long ONE visit may last before the cluster kills it. The workspace itself never expires — this is the session's limit, not the workspace's, and everything you leave behind survives it.">
            <NumberField value={minutes} onChange={setMinutes} min={1} placeholder="minutes per visit" ariaLabel="Time in minutes" />
          </Labeled>
          <Labeled label="GPUs" help="How many separate cards a visit holds at once, always on ONE machine. Memory above is the total across them.">
            <Select ariaLabel="GPUs" value={String(gpu)} onChange={(v) => setGpu(Number(v))} options={gpuOptions} />
          </Labeled>
        </div>

        <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3">
          <span aria-hidden />
          <button className="btn-primary justify-self-center" onClick={save} disabled={!ready}>
            {metaOnly ? "Save changes" : "Create workspace"}
          </button>
          <button className="btn-ghost justify-self-start" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
