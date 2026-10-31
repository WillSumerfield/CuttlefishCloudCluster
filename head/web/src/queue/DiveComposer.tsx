import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  createDive,
  errMessage,
  getDiveSettings,
  getFeatures,
  Unauthorized,
} from "../api";
import { useAuth } from "../app/auth";
import { navigate } from "../app/router";
import { TidepoolPicker } from "./TidepoolPicker";

export function DiveComposer() {
  const { me, logout } = useAuth();
  const [main, setMain] = useState("");
  const [loaded, setLoaded] = useState<string | null>(null);
  const [time, setTime] = useState("");
  const [vram, setVram] = useState("");
  const [gpus, setGpus] = useState("1");
  const [deps, setDeps] = useState<string[]>([]);
  const [env, setEnv] = useState("");
  const [features, setFeatures] = useState<string[]>([]);
  const [catalog, setCatalog] = useState<string[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [retry, setRetry] = useState(0);
  const [picker, setPicker] = useState<"main" | "deps" | "env" | null>(null);
  const [depPath, setDepPath] = useState("");
  const normalized = main
    .trim()
    .replace(new RegExp(`^/home/${me.username}/`), "")
    .replace(/^~\//, "")
    .replace(/\/+$/, "");

  useEffect(() => {
    let alive = true;
    getFeatures()
      .then((v) => {
        if (alive) setCatalog(v);
      })
      .catch((e) => {
        if (!alive) return;
        if (e instanceof Unauthorized) logout();
        else setCatalogError(errMessage(e));
      });
    return () => {
      alive = false;
    };
  }, [logout]);

  useEffect(() => {
    let alive = true;
    setLoaded(null);
    setError(null);
    setTime("");
    setVram("");
    setGpus("1");
    setDeps([]);
    setEnv("");
    setFeatures([]);
    setDepPath("");
    if (!normalized) return;
    const timer = window.setTimeout(() => {
      getDiveSettings(normalized)
        .then(({ main: folder, settings: s }) => {
          if (!alive) return;
          setTime(String(s.time ?? ""));
          setVram(String(s.vram ?? ""));
          setGpus(String(s.gpus ?? 1));
          setDeps(s.deps ?? []);
          setEnv(s.env ?? "");
          setFeatures(s.features ?? []);
          setLoaded(folder);
        })
        .catch((e) => {
          if (!alive) return;
          if (e instanceof Unauthorized) logout();
          else setError(errMessage(e));
        });
    }, 350);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [normalized, retry, logout]);

  const ready = loaded === normalized && !!loaded;
  const addDep = (path: string) => {
    const p = path.trim().replace(/^~\//, "").replace(/\/+$/, "");
    if (p && !deps.includes(p)) setDeps([...deps, p]);
    setDepPath("");
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ready || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const r = await createDive({
        main: loaded!,
        time,
        vram: Number(vram),
        gpus: Number(gpus),
        deps: depPath.trim()
          ? [
              ...new Set([
                ...deps,
                depPath.trim().replace(/^~\//, "").replace(/\/+$/, ""),
              ]),
            ]
          : deps,
        env: env.trim(),
        features,
      });
      // Notes from staging inputs remain visible on the destination page for this navigation.
      navigate(`/dive/${r.id}`, { diveNotes: r.notes });
      window.scrollTo(0, 0);
    } catch (e) {
      if (e instanceof Unauthorized) logout();
      else {
        const uncertain = !(e instanceof ApiError) || e.status >= 500;
        setError(
          errMessage(e) +
            (uncertain
              ? " Check the Queue before retrying; your Dive may already be queued."
              : ""),
        );
      }
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };

  return (
    <section className="surface p-10 min-w-0">
      <h2 className="label mb-5">Start a Dive</h2>
      <p className="text-[13px] text-ink-soft mb-5">
        Choose a Tidepool folder to work on with a GPU. Changes mirror back
        every minute.
      </p>
      <form onSubmit={submit} className="space-y-4">
        <fieldset disabled={busy} className="space-y-4 min-w-0">
          <label className="block space-y-2">
            <span className="label">Main folder</span>
            <div className="flex gap-2">
              <input
                className="field w-full min-w-0"
                aria-label="Main folder"
                placeholder="fractals"
                value={main}
                required
                onChange={(e) => {
                  setMain(e.target.value);
                  setPicker(null);
                }}
              />
              <button
                className="btn-ghost"
                type="button"
                onClick={() => setPicker(picker === "main" ? null : "main")}
              >
                Browse
              </button>
            </div>
          </label>
          {picker === "main" && (
            <TidepoolPicker
              onClose={() => setPicker(null)}
              onPick={(p) => {
                setMain(p);
                setPicker(null);
              }}
            />
          )}
          {normalized && !ready && !error && (
            <p role="status" className="text-[13px] text-ink-mute">
              Loading folder settings…
            </p>
          )}
          {error && (
            <div role="alert" className="text-[13px] text-alarm space-y-2">
              <p>{error}</p>
              {!ready && (
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => setRetry(retry + 1)}
                >
                  Reload settings
                </button>
              )}
            </div>
          )}
          <fieldset disabled={!ready} className="space-y-4 min-w-0">
            <p className="text-[12px] text-ink-mute">
              Choices are saved to this folder’s cuttle.toml when you start.
            </p>
            <label className="block space-y-2">
              <span className="label">Time limit</span>
              <input
                className="field w-full"
                required
                aria-label="Time limit"
                placeholder="4h, 90m, 2d · up to 14d"
                value={time}
                onChange={(e) => setTime(e.target.value)}
              />
            </label>
            <label className="block space-y-2">
              <span className="label">Total VRAM (GB)</span>
              <input
                className="field no-spinner w-full"
                required
                type="number"
                inputMode="numeric"
                min="1"
                step="1"
                aria-label="Total VRAM (GB)"
                placeholder="Across all requested GPUs"
                value={vram}
                onChange={(e) => setVram(e.target.value)}
              />
            </label>
            <label className="block space-y-2">
              <span className="label">GPUs</span>
              <input
                className="field no-spinner w-full"
                required
                type="number"
                inputMode="numeric"
                min="1"
                step="1"
                aria-label="GPUs"
                value={gpus}
                onChange={(e) => setGpus(e.target.value)}
              />
            </label>
            <div className="space-y-2">
              <span className="label">Read-only folders</span>
              <p className="text-[12px] text-ink-mute">
                Extra Tidepool folders to bring along. Changes do not mirror
                back.
              </p>
              {deps.map((d) => (
                <div key={d} className="flex items-center gap-2 text-[13px]">
                  <span className="break-all">~/{d}</span>
                  <button
                    type="button"
                    className="btn-ghost ml-auto"
                    aria-label={`Remove ${d}`}
                    onClick={() => setDeps(deps.filter((p) => p !== d))}
                  >
                    Remove
                  </button>
                </div>
              ))}
              <div className="flex gap-2">
                <input
                  className="field w-full min-w-0"
                  aria-label="Read-only folder"
                  placeholder="datasets/mnist"
                  value={depPath}
                  onChange={(e) => setDepPath(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      addDep(depPath);
                    }
                  }}
                />
                <button
                  type="button"
                  className="btn-ghost"
                  disabled={!depPath.trim()}
                  onClick={() => addDep(depPath)}
                >
                  Add
                </button>
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => setPicker(picker === "deps" ? null : "deps")}
                >
                  Browse
                </button>
              </div>
              {picker === "deps" && (
                <TidepoolPicker
                  onClose={() => setPicker(null)}
                  onPick={(p) => {
                    addDep(p);
                    setPicker(null);
                  }}
                />
              )}
            </div>
            <label className="block space-y-2">
              <span className="label">Environment file</span>
              <p className="text-[12px] text-ink-mute">
                A dependency file inside the main folder, such as
                pyproject.toml.
              </p>
              <div className="flex gap-2">
                <input
                  className="field w-full min-w-0"
                  aria-label="Environment file"
                  placeholder="pyproject.toml"
                  value={env}
                  onChange={(e) => setEnv(e.target.value)}
                />
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => setPicker(picker === "env" ? null : "env")}
                >
                  Browse
                </button>
              </div>
            </label>
            {picker === "env" && (
              <TidepoolPicker
                key={loaded}
                root={loaded!}
                files
                onClose={() => setPicker(null)}
                onPick={(p) => {
                  setEnv(p.slice(loaded!.length + 1));
                  setPicker(null);
                }}
              />
            )}
            <div className="space-y-2">
              <span className="label">Features</span>
              {catalogError && (
                <p role="alert" className="text-[13px] text-alarm">
                  Could not load Features. {catalogError}
                </p>
              )}
              {[...new Set([...catalog, ...features])].map((f) => (
                <label key={f} className="flex items-center gap-2 text-[13px]">
                  <input
                    type="checkbox"
                    checked={features.includes(f)}
                    onChange={(e) =>
                      setFeatures(
                        e.target.checked
                          ? [...features, f]
                          : features.filter((v) => v !== f),
                      )
                    }
                  />
                  {f}
                </label>
              ))}
              {!catalogError && !catalog.length && (
                <p className="text-[12px] text-ink-mute">
                  No Features available.
                </p>
              )}
            </div>
          </fieldset>
          {busy && (
            <p role="status" className="text-[13px] text-ink-soft">
              Snapshotting your Tidepool and queueing the Dive…
            </p>
          )}
          <button
            className="btn-primary"
            disabled={!ready || busy}
            type="submit"
          >
            {busy ? "Creating Dive…" : "Start Dive"}
          </button>
        </fieldset>
      </form>
    </section>
  );
}
