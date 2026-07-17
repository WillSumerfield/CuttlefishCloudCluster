import { useCallback, useRef, useState } from "react";
import type { Arm } from "../api";
import { submit } from "../api";
import { human, pick, type Picked } from "../project";

/**
 * Submit a Spawn. The affordance IS the form: an empty strip that says "drop a project or choose a
 * folder", and picking one expands the composer inline. That expansion is also the PREVIEW GATE —
 * name, file count, total size — and nothing uploads until Submit. Bytes move on one click, never on
 * the pick.
 *
 * Source is folder today; a Recipe is the other way in (Ph5) and will drop into the same composer.
 * That's why the picked project is held behind `source`, not assumed to be a folder everywhere.
 */

const GB = 1024 ** 3;

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
  const [picked, setPicked] = useState<Picked | null>(null);
  const [honor, setHonor] = useState(true);
  const [script, setScript] = useState("");
  const [gpu, setGpu] = useState(1);
  const [minutes, setMinutes] = useState("");
  const [vram, setVram] = useState("");
  const [node, setNode] = useState("");
  const [args, setArgs] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const lastList = useRef<FileList | null>(null);

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
    setScript("");
    setMinutes("");
    setVram("");
    setArgs("");
    setNode("");
    if (fileRef.current) fileRef.current.value = "";
  };

  const ready = picked && script && Number(minutes) > 0 && Number(vram) > 0;

  const go = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await submit({
        picked,
        script,
        gpu,
        minutes: Number(minutes),
        vram: Number(vram),
        node: node || null,
        args: args.split(/\s+/).filter(Boolean),
      });
      reset();
      onSubmitted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const open = startOpen || picked !== null;

  return (
    <section className="surface [--sink:35%] mb-10" style={{ padding: "calc(var(--pad) + 24px) calc(var(--pad) + 50px)" }}>
      {/* The strip — a recess you drop a folder onto, or click to choose one. */}
      <label
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className="flex cursor-pointer items-center justify-center rounded-[12px] px-6 py-7 text-center transition-[background] duration-200"
        style={{
          background: dragging
            ? "color-mix(in oklab, var(--irid-low) 14%, var(--well))"
            : "color-mix(in oklab, var(--well) 70%, black)",
        }}
      >
        <input
          ref={fileRef}
          type="file"
          onChange={onPick}
          className="sr-only"
          {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
        />
        <span className="text-[15px] text-ink-soft">
          {picked ? (
            <>
              <span className="num text-ink">{picked.root}</span> — choose a different folder
            </>
          ) : (
            <>Drop a project here, or choose a folder</>
          )}
        </span>
      </label>

      {open && (
        <div className="mt-6 space-y-5">
          {/* The preview gate. Everything the pick told us, and the promise that nothing has moved. */}
          {picked && (
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 px-1 text-[13px]">
              <span className="text-ink-soft">
                <span className="num text-ink">{picked.files.length}</span> of{" "}
                <span className="num">{picked.scanned}</span> files,{" "}
                <span className="num text-ink">{human(picked.bytes)}</span> will upload
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

          {picked && picked.bytes > GB && (
            <p className="rounded-[10px] px-3 py-2 text-[13px]" style={{ background: "color-mix(in oklab, var(--alarm) 12%, transparent)", color: "var(--ink-soft)" }}>
              That's over a gigabyte. Check your .gitignore excludes data and checkpoints — the upload
              caps at 4&nbsp;GB.
            </p>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <Labeled label="Script" className="sm:col-span-2">
              <input className="field" placeholder="train.py" value={script} onChange={(e) => setScript(e.target.value)} />
            </Labeled>
            <Labeled label="Memory">
              <input className="field" type="number" min={1} placeholder="VRAM, GB" value={vram} onChange={(e) => setVram(e.target.value)} />
            </Labeled>
            <Labeled label="Time">
              <input className="field" type="number" min={1} placeholder="minutes" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
            </Labeled>
            <Labeled label="GPUs">
              <input className="field" type="number" min={0} value={gpu} onChange={(e) => setGpu(Number(e.target.value))} />
            </Labeled>
            {/* Naming a machine is an Admin power — a User says what they NEED and the scheduler places
                it. Non-admins never see this (and the Head 403s it). */}
            {isAdmin && (
              <Labeled label="Arm">
                <select className="field" value={node} onChange={(e) => setNode(e.target.value)}>
                  <option value="">Any eligible</option>
                  {arms.map((a) => (
                    <option key={a.name} value={a.name}>
                      {a.name}
                    </option>
                  ))}
                </select>
              </Labeled>
            )}
            <Labeled label="Arguments" className={isAdmin ? "sm:col-span-2" : "sm:col-span-2"}>
              <input className="field" placeholder="optional" value={args} onChange={(e) => setArgs(e.target.value)} />
            </Labeled>
          </div>

          {error && <p className="text-[13px] text-[var(--alarm)]">{error}</p>}

          <div className="flex items-center gap-3">
            <button className="btn-primary" onClick={go} disabled={!ready || busy}>
              {busy ? "Submitting…" : "Submit spawn"}
            </button>
            {picked && (
              <button className="btn-ghost" onClick={reset} disabled={busy}>
                Clear
              </button>
            )}
            <span className="text-[12px] text-ink-mute">
              Tracking is set up on your Account. {/* W&B deferred until the Account page + key store */}
            </span>
          </div>
        </div>
      )}
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
