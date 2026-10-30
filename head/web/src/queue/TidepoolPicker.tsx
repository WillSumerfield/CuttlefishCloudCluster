import { useEffect, useState } from "react";
import { listFiles, Unauthorized, errMessage, type FileEntry } from "../api";
import { useAuth } from "../app/auth";

/** A small read-only browser shared by main folders, inputs and environment files. */
export function TidepoolPicker({
  root = ".",
  files = false,
  onPick,
  onClose,
}: {
  root?: string;
  files?: boolean;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const { logout } = useAuth();
  const [dir, setDir] = useState(root);
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setEntries(null);
    setError(null);
    listFiles(dir)
      .then((r) => {
        if (alive) setEntries(r.entries);
      })
      .catch((e) => {
        if (!alive) return;
        if (e instanceof Unauthorized) logout();
        else setError(errMessage(e));
      });
    return () => {
      alive = false;
    };
  }, [dir, logout]);
  return (
    <div className="field space-y-3 p-4" aria-label="Browse Tidepool">
      <div className="flex flex-wrap items-center gap-2">
        <span className="num text-[12px] break-all">
          {dir === "." ? "~" : `~/${dir}`}
        </span>
        <button className="btn-ghost ml-auto" type="button" onClick={onClose}>
          Close
        </button>
      </div>
      {dir !== root && (
        <button
          className="btn-ghost"
          type="button"
          onClick={() =>
            setDir(dir.includes("/") ? dir.slice(0, dir.lastIndexOf("/")) : ".")
          }
        >
          ↑ Parent folder
        </button>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-alarm">
          {error}
        </p>
      )}
      {!error && entries === null && (
        <p role="status" className="text-[13px] text-ink-mute">
          Loading folders…
        </p>
      )}
      <ul className="max-h-52 overflow-y-auto space-y-1">
        {entries
          ?.filter((e) => e.type === "dir" || (files && e.type === "file"))
          .map((e) => (
            <li key={e.path}>
              <button
                type="button"
                className="btn-ghost w-full text-left break-all"
                onClick={() =>
                  e.type === "dir" ? setDir(e.path) : onPick(e.path)
                }
              >
                {e.type === "dir" ? `${e.name}/` : e.name}
                {e.locked ? " · in use" : ""}
              </button>
            </li>
          ))}
      </ul>
      {!files && dir !== "." && (
        <button
          type="button"
          className="btn-primary"
          onClick={() => onPick(dir)}
        >
          Choose this folder
        </button>
      )}
    </div>
  );
}
