import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Arm, Recipe } from "../api";
import { getFeatureCatalog, getWandbKey, submit, submitTidepool } from "../api";
import { human, pick, relPath, type Picked } from "../project";
import { NumberField } from "./NumberField";
import { Chip } from "./Chip";
import { Select } from "./Select";
import { Tip } from "./Tip";
import { Link } from "./router";
import { ALLOWANCE, size } from "../recipes/format";

/**
 * The Spawn composer and the Recipe composer, as one implementation. They ask nearly the same questions — pick a folder, name a script, declare needs — so `variant` is
 * the one thing that tells them apart, not two copies of the same state machine drifting out of sync.
 * `queue/SubmitComposer.tsx` and `recipes/RecipeComposer.tsx` are thin wrappers over this.
 *
 * What actually differs, and why it stays branched rather than unified further:
 *   · Name — optional for a Spawn (falls back to the script's name); required, unique, and fixed once
 *     `editing`, for a Recipe (it's the key you Run it by later).
 *   · Tide Pool mode and admin node-targeting exist for a Spawn only — neither means anything for a
 *     Recipe (CONTEXT.md: it runs from any machine with nothing to hand, so pinning a node contradicts
 *     the point, and there is no "live shell" to freeze).
 *   · The 32GB wall is a Recipe-only hard gate, checked from `File.size` at pick time.
 *   · W&B is gated on whether the User has a key stored for a Spawn (it runs NOW); ungated for a
 *     Recipe, which is durable config that outlives today's answer.
 *   · Saving: a Spawn composer performs its own submit (busy/error/reset live here). A Recipe composer
 *     hands a validated payload to `onSave` — the page owns the optimistic card and the wall preview,
 *     because a Recipe's save has to update a shelf of many, not one queue.
 */

const GB = 1024 ** 3;

// Mirrors the Head's OUTPUT_NAME_RE (ccc_head_api.spawns) for both — an output name becomes a shell-
// interpolated `mkdir -p "$ccc_out/<name>"`. A client-side reject is instant; the Head's is the one
// that actually matters (it never trusts this one).
const OUTPUT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

// The recipe composer card's own corner radius and alpha edge-fade depth (px) — tune these, not
// `.shard`'s global --radius-surface/--fade-x/--fade-y, to change this card without touching every other
// shard in the app. Exported so ComposerSlot's growing/shrinking backdrop can use the exact same numbers
// — otherwise the border would look right once settled but mismatched while still fading.
//
// RADIUS only reads clearly once it's large relative to FADE — the fade's dissolve zone reaches 2×FADE
// in from each edge, and a radius smaller than that gets swamped by it (verified: at FADE 32, RADIUS 1
// vs 16 are visually indistinguishable; RADIUS 128 clearly is not).
export const COMPOSER_RADIUS = 64;
export const COMPOSER_FADE = 32;

export type RecipePayload = {
  picked: Picked;
  name: string;
  script: string;
  args: string[];
  outputs: string[];
  gpus: number;
  vram_mb: number;
  minutes: number;
  wandb: boolean;
  wandb_project: string;
  wandb_entity: string;
  manifest: string;
  features: string[];
};

// Same shape minus `picked`/`name` — a settings-only save touches neither (no fresh code, and a Recipe's
// name is fixed once it exists). `editing` must be set to use this at all: metaOnly with no `editing`
// would have nothing to fall back to for a folder that was never picked.
export type RecipeSettingsPayload = Omit<RecipePayload, "picked" | "name">;

type SpawnProps = {
  variant: "spawn";
  arms: Arm[];
  isAdmin: boolean;
  /** Open on load when the queue is empty — an empty screen is an invitation to act. */
  startOpen?: boolean;
  onSubmitted: () => void;
};

type RecipeProps = {
  variant: "recipe";
  /** The fleet, for the same reason a Spawn needs it: the GPU-count dropdown greys out counts no
   *  single Arm can host. A Recipe is blocked at save exactly as a Spawn is at submit — freezing a
   *  need nothing can satisfy just moves the refusal to the day it's finally run. */
  arms: Arm[];
  /** Bytes already committed to the wall — the fit test adds the picked folder to this. */
  used: number;
  /** Names already taken on this shelf; a Recipe's name is its key, so it must be unique. */
  existingNames: string[];
  /** The Recipe being re-captured, or undefined for a new one. Editing an existing Recipe never REQUIRES
   *  a folder — dropping one is optional and, only if used, also refreshes the code (`onSave`); otherwise
   *  Save changes settings alone (`onSaveMeta`, `script` free-typed against the code already saved). */
  editing?: Recipe;
  /** Report the picked folder's bytes up so the page can preview them as a ghost on the wall. */
  onPending: (bytes: number) => void;
  onSave: (r: RecipePayload) => void;
  /** Required whenever `editing` is set — the no-folder-dropped save path. */
  onSaveMeta?: (r: RecipeSettingsPayload) => void;
  onCancel: () => void;
};

export type ComposerProps = SpawnProps | RecipeProps;

export function Composer(props: ComposerProps) {
  const { variant } = props;
  const recipe = variant === "recipe" ? props : null;
  const spawn = variant === "spawn" ? props : null;
  const editing = recipe?.editing;
  // Whether a folder is OPTIONAL rather than required — true for every existing Recipe (there's already
  // code on disk `script` can be checked against without one), false only for a brand-new one.
  const metaOnly = !!editing;

  // The folder-dropper's own knobs — corner radius, blur feather (a blur feathers the true perimeter;
  // see either twin's prior history for why that beat the surfaces' crossed-linear-ramp mask), and its
  // own padding. Independent of COMPOSER_RADIUS/COMPOSER_FADE above, which are the card's, not the
  // dropper's. Split per-variant in case they ever need to diverge again; identical for now.
  const DROP_FADE =
    variant === "recipe"
      ? { radius: 16, feather: 8, padX: 16, padY: 16 }
      : { radius: 16, feather: 8, padX: 24, padY: 32 };

  // The container's own edge inset — space between ITS border and everything inside it (heading, strip,
  // every field), uniformly, not space between the container and whatever holds it (that's still fixed:
  // `h-full`/ComposerSlot for a Recipe, `mb-10` for a Spawn). Defaults match what was previously hardcoded
  // inline (3rem = 48px for a Recipe; Spawn keeps its `var(--pad)` page-density base, offset by 50px).
  const CONTAINER_PAD = variant === "recipe" ? { x: 64, y: 64 } : { x: 50, y: 64 };

  const [mode, setMode] = useState<"script" | "tidepool">("script");
  const [picked, setPicked] = useState<Picked | null>(null);
  const [honor, setHonor] = useState(true);
  const [name, setName] = useState(editing?.name ?? "");
  const [script, setScript] = useState(editing?.script ?? "");
  // Clamped: a Recipe frozen before CCC required a Device could carry 0, and 0 is no longer a count
  // this control can even offer.
  const [gpu, setGpu] = useState(Math.max(1, editing?.gpus ?? 1));
  const [minutes, setMinutes] = useState(editing ? String(editing.minutes) : "");
  const [vram, setVram] = useState(editing ? String(editing.vram_mb / 1024) : "");
  const [node, setNode] = useState("");
  const [args, setArgs] = useState(editing?.args.join(" ") ?? "");
  const [outputs, setOutputs] = useState<string[]>(editing?.outputs ?? []);
  const [outputDraft, setOutputDraft] = useState("");
  const [outputError, setOutputError] = useState<string | null>(null);
  const [featureCatalog, setFeatureCatalog] = useState<string[]>([]);
  const [features, setFeatures] = useState<string[]>(editing?.features ?? []);
  const [manifest, setManifest] = useState(editing?.manifest ?? "");
  // What the Head says it will build, echoed after a successful Spawn submit — meaningless for a
  // Recipe, which never runs at save time.
  const [envNote, setEnvNote] = useState<string | null>(null);
  const [wandbLog, setWandbLog] = useState(editing?.wandb ?? true);
  const [wandbProject, setWandbProject] = useState(editing?.wandb_project ?? "");
  const [wandbEntity, setWandbEntity] = useState(editing?.wandb_entity ?? "");
  // Whether W&B is on for this User at all — Spawn-only (a Recipe asks regardless; see the module
  // docstring). Null until the Head answers; a failed read shows the fields rather than blocking.
  const [wandbOn, setWandbOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tidepoolId, setTidepoolId] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const lastList = useRef<FileList | null>(null);

  useEffect(() => {
    if (variant !== "spawn") return;
    getWandbKey()
      .then((s) => setWandbOn(s.set))
      .catch(() => setWandbOn(true)); // unreadable → assume on; the Head decides for real at submit
  }, [variant]);

  // The catalog is fetched once, independent of what's picked — what the Head knows exists, not what
  // this composer happens to be building right now.
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
  const arms = (spawn?.arms ?? recipe!.arms).filter((a) => a.admitted && !a.retracted);

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
      disabled: known && !arms.some((a) => a.gpus >= n && (a.vram_mb ?? 0) / 1024 >= need),
    };
  });

  const setPick = useCallback(
    (p: Picked | null) => {
      setPicked(p);
      recipe?.onPending(p?.bytes ?? 0);
    },
    [recipe],
  );

  // The script picker's choices: every .py in the picked tree, so "script must exist" is true by
  // construction. Sorted so a re-pick doesn't reshuffle the list.
  const scriptOptions = useMemo(
    () => (picked ? picked.files.filter((f) => f.name.endsWith(".py")).map(relPath).sort() : []),
    [picked],
  );

  // A repick can drop the previously-selected script from the tree. Cleared here rather than letting
  // the dropdown silently fall back to its first option while `script` state still holds the stale
  // path. Recipe-only wrinkle: `editing`'s script must survive until a folder is actually (re-)picked,
  // not just because the fresh tree (still empty) doesn't contain it yet.
  useEffect(() => {
    if (script && !scriptOptions.includes(script) && (variant === "spawn" || picked)) setScript("");
  }, [scriptOptions, script, picked, variant]);

  const rescan = useCallback(
    async (list: FileList | null, honorGitignore: boolean) => {
      if (!list || list.length === 0) return;
      setPick(await pick(list, honorGitignore));
    },
    [setPick],
  );

  const onPickFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
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

  // Spawn-only: clears the whole form and stays open, ready for another. Recipe's equivalent
  // (`cancel` below) closes the composer instead — there's no "another" to stay open for.
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
    if (fileRef.current) fileRef.current.value = "";
  };

  const cancel = () => {
    recipe?.onPending(0);
    recipe?.onCancel();
  };

  const trimmed = name.trim();
  const dup = variant === "recipe" && recipe!.existingNames.includes(trimmed);
  const projected = (picked?.bytes ?? 0) + (recipe?.used ?? 0);
  // Only real once a folder is actually picked — with none dropped, nothing new is being weighed (a
  // metaOnly save touches no bytes at all), and projected is just `used`, which already fit.
  const over = variant === "recipe" && !!picked && projected > ALLOWANCE;
  const overBy = projected - ALLOWANCE;

  // A dropped folder always means "also refresh the code" — same requirements either way, metaOnly or
  // not (a brand-new Recipe always has one; editing's is just optional). Only when editing WITHOUT one
  // does readiness relax to the settings-only shape, `script` free-typed rather than picked from a tree.
  //
  // `manifest` gates every branch but the Tide Pool's: naming the dependency file is required, because
  // an Environment carries its own Python and inherits nothing, so the Head refuses a Spawn or a Recipe
  // that named none. A Tide Pool is the one Spawn that may decline — it is a shell, not a run.
  const named = !!manifest.trim();
  // The selected count can go stale without being re-picked: choose 2 cards at 40GB, then raise Memory
  // to 400, and the option that was fine is now one no Arm can host. Greying the row out doesn't undo
  // a choice already made, so readiness re-asks the same question of what is currently selected.
  // A count past the end of the list is unplaceable too, not merely unlisted — an old Recipe frozen at
  // 4 cards, opened after the 4-card Arm left, has no row here at all.
  const placeable = !known || !!gpuOptions[gpu - 1] && !gpuOptions[gpu - 1].disabled;
  const ready = placeable && (
    variant === "spawn" && mode === "tidepool"
      // A named manifest needs the folder that holds it — the Head builds from the upload, and there
      // is nothing to resolve the name against otherwise.
      ? Number(minutes) > 0 && Number(vram) > 0 && (!named || !!picked)
      : variant === "recipe"
        ? metaOnly && !picked
          ? !!script.trim() && named && Number(minutes) > 0 && Number(vram) > 0 && outputs.length > 0
          : !!picked && !!trimmed && !dup && !!script && named && Number(minutes) > 0 &&
            Number(vram) > 0 && outputs.length > 0 && !over
        : !!picked && !!script && named && Number(minutes) > 0 && Number(vram) > 0 &&
          outputs.length > 0);

  const go = async () => {
    if (!ready || variant !== "spawn") return;
    setBusy(true);
    setError(null);
    setEnvNote(null);
    try {
      if (mode === "tidepool") {
        const id = await submitTidepool({
          minutes: Number(minutes), vram: Number(vram), gpu, node: node || null, features, picked,
          name: name.trim() || null, manifest: manifest.trim(),
        });
        setTidepoolId(id);
      } else {
        const res = await submit({
          picked: picked as Picked, name: name.trim() || null, script, gpu, minutes: Number(minutes),
          vram: Number(vram), node: node || null, args: args.split(/\s+/).filter(Boolean),
          wandb: wandbLog, wandbProject, wandbEntity, outputs, features, manifest,
        });
        setEnvNote(res.environment);
      }
      reset();
      spawn!.onSubmitted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    if (!ready || variant !== "recipe") return;
    const settings = {
      script, args: args.split(/\s+/).filter(Boolean), outputs, gpus: gpu,
      vram_mb: Number(vram) * 1024, minutes: Number(minutes), wandb: wandbLog,
      wandb_project: wandbProject.trim(), wandb_entity: wandbEntity.trim(), manifest: manifest.trim(),
      features,
    };
    if (metaOnly && !picked) {
      recipe!.onSaveMeta!(settings);
      return;
    }
    if (!picked) return;
    recipe!.onSave({ picked, name: trimmed, ...settings });
  };

  const open = variant === "spawn" ? (spawn!.startOpen || picked !== null || mode === "tidepool") : true;

  const fields = (
    <div className="mt-6 space-y-5">
      {/* The preview gate. Everything the pick told us, and the promise that nothing has moved. Two
          different tones on purpose: a Spawn's upload is a one-off (all-files-vs-gitignore is a live
          toggle you might flip either way); a Recipe's is a durable commitment (a plain checkbox,
          framed against the allowance it's about to hold). */}
      {picked && variant === "spawn" && (
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
      {picked && variant === "recipe" && (
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

      {/* The hard wall — Recipe only, blocked before the upload. Spawn has no wall, only an advisory
          note past 1GB (the upload caps at 4GB regardless). */}
      {over && (
        <p className="rounded-[10px] px-3 py-2 text-[13px]" style={{ background: "color-mix(in oklab, var(--alarm) 12%, transparent)", color: "var(--ink-soft)" }}>
          <span className="num" style={{ color: "var(--alarm)" }}>{size(overBy)}</span> over your{" "}
          <span className="num">{size(ALLOWANCE)}</span> — delete a recipe to make room.
        </p>
      )}
      {picked && picked.bytes > GB && variant === "spawn" && (
        <p className="rounded-[10px] px-3 py-2 text-[13px]" style={{ background: "color-mix(in oklab, var(--alarm) 12%, transparent)", color: "var(--ink-soft)" }}>
          That's over a gigabyte. Check your .gitignore excludes data and checkpoints — the upload
          caps at 4&nbsp;GB.
        </p>
      )}
      {picked && !over && picked.bytes > GB && variant === "recipe" && (
        <p className="rounded-[10px] px-3 py-2 text-[13px]" style={{ background: "color-mix(in oklab, var(--irid-low) 10%, transparent)", color: "var(--ink-soft)" }}>
          That's over a gigabyte held on the Head for good. Check your .gitignore excludes data and
          checkpoints.
        </p>
      )}

      <div className={variant === "recipe" ? "grid gap-3" : "grid gap-3 sm:grid-cols-2"}>
        <Labeled label="Name" className="sm:col-span-2" help={variant === "recipe"
          ? "What you'll call this Recipe when you run it later. It has to be unique among yours."
          : "An optional label so you can recognise this run in the queue later. Leave it blank and the script's name is used."}>
          <input
            className="field"
            placeholder="optional"
            value={name}
            readOnly={variant === "recipe" && !!editing}
            onChange={(e) => setName(e.target.value)}
          />
          {dup && <span className="mt-1 block text-[12px]" style={{ color: "var(--alarm)" }}>You already have a recipe named {trimmed}.</span>}
        </Labeled>

        {(variant === "recipe" || mode === "script") && (
          <>
            <Labeled label="Script" className="sm:col-span-2" help={metaOnly && !picked
              ? "The file the cluster actually runs, relative to this recipe's saved code. Drop a folder above to pick a different entrypoint from a fresh tree instead."
              : "The file the cluster actually runs, relative to the folder you picked."}>
              {metaOnly && !picked ? (
                <input
                  className="field"
                  placeholder="path/to/script.py"
                  value={script}
                  onChange={(e) => setScript(e.target.value)}
                />
              ) : scriptOptions.length > 0 ? (
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
            <Labeled label="Arguments" className="sm:col-span-2" help="Passed to your script exactly as typed, the same as if you had run it yourself.">
              <input className="field" placeholder="optional" value={args} onChange={(e) => setArgs(e.target.value)} />
            </Labeled>
            <Labeled label="Output folders" className="sm:col-span-2" help="Folders your results are written into. Anything written outside them is recoverable for a day after the Spawn ends and then gone, so name the right ones rather than relying on that. What lands in them is kept 7 days.">
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
            {/* W&B. Gated on whether the User has a key, for a Spawn only — it runs NOW, so the now-
                state is the right thing to gate it on. A Recipe is durable config that outlives it:
                hiding these because there's no key today would freeze a Recipe with no project for a
                reason that stops being true the moment a key is pasted in. */}
            {variant === "recipe" ? (
              <Wandb log={wandbLog} setLog={setWandbLog} project={wandbProject} setProject={setWandbProject}
                    entity={wandbEntity} setEntity={setWandbEntity} />
            ) : wandbOn === false ? (
              <p className="text-[13px] text-ink-mute sm:col-span-2 text-center py-1">
                <Link to="/account" className="text-[var(--gold)] underline-offset-4 hover:underline">
                  Add a key for W&amp;B
                </Link>
              </p>
            ) : wandbOn === true ? (
              <Wandb log={wandbLog} setLog={setWandbLog} project={wandbProject} setProject={setWandbProject}
                    entity={wandbEntity} setEntity={setWandbEntity} />
            ) : null}
          </>
        )}

        {/* Free text, and deliberately not a list of the dependency files we can see in the picked
            folder: offering one would be CCC deciding which file is the instruction, and a suggestion
            is an inference the moment anyone accepts it.

            Shown for a Tide Pool too, where it is the one OPTIONAL field of its kind — a Tide Pool may
            decline to declare, and one opened to poke at a box has no project behind it to name a file
            in. Naming one there builds that env into the shell, so what you debug in is what your
            Spawn would have run in. Required everywhere else — see `named`. */}
        <Labeled label="Dependencies" className="sm:col-span-2" help={mode === "tidepool"
          ? "Optional here, and only here. Name a dependency file and this shell gets the environment your Spawn would have run in — pick a folder above too, since it cannot be built from a file that was not sent. Leave it blank for a bare shell."
          : "Which file builds your Python environment, relative to your project root. Required: the environment carries its own Python and inherits nothing from the cluster, so what it is built from is yours to name."}>
          <input
            className="field"
            placeholder={mode === "tidepool"
              ? "optional — pyproject.toml, requirements.txt…"
              : "pyproject.toml, uv.lock, requirements.txt, environment.yml…"}
            value={manifest}
            onChange={(e) => setManifest(e.target.value)}
          />
          {mode === "tidepool" && named && !picked && (
            <span className="mt-1 block text-[12px]" style={{ color: "var(--alarm)" }}>
              Pick a folder above — the environment is built from what you upload.
            </span>
          )}
        </Labeled>

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
        {/* Naming a machine is an Admin power, and only means anything for a Spawn — a Recipe never
            targets one (CONTEXT.md: it runs from any machine with nothing to hand). */}
        {variant === "spawn" && spawn!.isAdmin && (
          <Labeled label="Arm" help="Admin only. Everyone else declares what they need and the cluster places the work — naming a machine is how a queue stops being fair.">
            <Select
              ariaLabel="Target an arm"
              value={node}
              onChange={setNode}
              options={[{ value: "", label: "Any eligible" }, ...spawn!.arms.map((a) => ({ value: a.name, label: a.name }))]}
            />
          </Labeled>
        )}
      </div>

      {error && <p className="text-[13px] text-[var(--alarm)]">{error}</p>}
      {envNote && <p className="text-[13px] text-[var(--muted)]">{envNote}</p>}

      {/* Primary sits dead-centre; the secondary hangs off its right without shifting it. */}
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3">
        <span aria-hidden />
        {variant === "spawn" ? (
          <button className="btn-primary justify-self-center" onClick={go} disabled={!ready || busy}>
            {busy
              ? mode === "tidepool" ? "Requesting…" : "Submitting…"
              : mode === "tidepool" ? "Request tide pool" : "Submit spawn"}
          </button>
        ) : (
          <button className="btn-primary justify-self-center" onClick={save} disabled={!ready}>
            {metaOnly ? "Save changes" : "Save recipe"}
          </button>
        )}
        {variant === "spawn" ? (
          picked ? (
            <button className="btn-ghost justify-self-start" onClick={reset} disabled={busy}>
              Clear
            </button>
          ) : (
            <span aria-hidden />
          )
        ) : (
          <button className="btn-ghost justify-self-start" onClick={cancel}>
            Cancel
          </button>
        )}
      </div>
    </div>
  );

  const strip = (
    <label
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className="relative flex cursor-pointer items-center justify-center text-center"
      style={{
        borderRadius: DROP_FADE.radius,
        minHeight: DROP_FADE.radius * 2,
        // padX is MARGIN, not the strip's own padding: padding would only push its contents inward
        // without changing its own box, but the ask is for the box itself to narrow, leaving a real gap
        // to the card's edges. padY stays internal — only the width is meant to respond here.
        marginInline: DROP_FADE.padX,
        padding: `${DROP_FADE.padY}px 0`,
      }}
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
      <span className={`relative text-[15px] ${picked ? "text-ink-soft" : "text-ink-mute"}`}>
        {picked ? (
          <span className="num text-ink">{picked.root}</span>
        ) : variant === "spawn" && mode === "tidepool" ? (
          <>Drop/choose a folder — optional</>
        ) : metaOnly ? (
          <>Drop/choose a folder to also refresh the code — optional</>
        ) : (
          <>Drop/choose a folder</>
        )}
      </span>
    </label>
  );

  if (variant === "recipe") {
    // ComposerSlot (its parent, RecipesPage.tsx) owns the actual grid item now — this only ever mounts
    // inside one, already sized row-span-2 by that shell.
    return (
      <div
        className="shard [--sink:35%] h-full"
        style={{
          padding: `${CONTAINER_PAD.y}px ${CONTAINER_PAD.x}px`,
          // --radius-surface, not a direct borderRadius: the visible plate is painted by .surface's
          // OWN ::before pseudo-element (index.css), which reads var(--radius-surface) itself — an
          // inline style on this div can't reach a pseudo-element at all, only a CSS custom property
          // inherited down to it can. Same reason --fade-x/--fade-y (below) already worked as overrides
          // and a plain `borderRadius` here silently didn't.
          "--radius-surface": `${COMPOSER_RADIUS}px`,
          "--fade-x": `${COMPOSER_FADE}px`,
          "--fade-y": `${COMPOSER_FADE}px`,
        } as React.CSSProperties}
      >
        <h2 className="label mb-6">{metaOnly ? `Edit ${editing!.name}` : "New recipe"}</h2>
        {metaOnly && picked && (
          <p className="mb-6 px-1 text-[13px] text-ink-mute">
            Re-freezing this recipe around a new folder. It keeps running the old snapshot until this
            lands, and its <span className="num">{size(editing!.size_bytes)}</span> comes off the wall
            when it does.
          </p>
        )}
        {strip}
        {fields}
      </div>
    );
  }

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
        {strip}
        {open && fields}
      </section>
    </>
  );
}

function Wandb({ log, setLog, project, setProject, entity, setEntity }: {
  log: boolean; setLog: (v: boolean) => void;
  project: string; setProject: (v: string) => void;
  entity: string; setEntity: (v: string) => void;
}) {
  return (
    <>
      <div className="sm:col-span-2">
        <span className="label mb-1.5 block">Use W&amp;B?</span>
        <Tip
          className="block"
          focusable={false}
          text="Whether this run logs to Weights &amp; Biases. Your stored key is used for every Spawn unless you turn it off here."
        >
          <div className="flex gap-1">
            <Chip on={log} onClick={() => setLog(true)}>Enabled</Chip>
            <Chip on={!log} onClick={() => setLog(false)}>Disabled</Chip>
          </div>
        </Tip>
      </div>
      {log && (
        <>
          <Labeled label="W&B project" help="Which W&B project this run logs into. Blank uses the cluster's default.">
            <input className="field" placeholder="ccc" value={project} onChange={(e) => setProject(e.target.value)} />
          </Labeled>
          <Labeled label="W&B entity" help="The W&B team or account the run logs under. Blank uses whatever your key's account defaults to.">
            <input className="field" placeholder="account default" value={entity} onChange={(e) => setEntity(e.target.value)} />
          </Labeled>
        </>
      )}
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
