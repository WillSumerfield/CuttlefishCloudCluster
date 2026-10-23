import { useCallback, useEffect, useRef, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { Tip } from "../app/Tip";
import {
  deleteFile, downloadUrl, errMessage, getTidepool, listFiles, makeFolder, renameFile, Unauthorized,
  uploadFile, type FileEntry, type Tidepool,
} from "../api";

// NAME · SIZE · MODIFIED · ACTIONS
const COLS = "minmax(180px,1fr) 90px 150px 210px";

const fmtSize = (b: number | null) => {
  if (b === null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
};
const fmtTime = (t: number) => new Date(t * 1000).toLocaleString();
const join = (dir: string, name: string) => (dir === "." ? name : `${dir}/${name}`);

type Pending = { path: string; file: File };

/** Every file under a dropped item, with its path relative to the drop. */
async function walk(entry: FileSystemEntry, prefix: string): Promise<Pending[]> {
  if (entry.isFile) {
    const file = await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej));
    return [{ path: prefix + entry.name, file }];
  }
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  const kids: FileSystemEntry[] = [];
  // readEntries answers in batches until it answers empty.
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) break;
    kids.push(...batch);
  }
  const out = await Promise.all(kids.map((k) => walk(k, `${prefix}${entry.name}/`)));
  return out.flat();
}

/**
 * Files — your Tidepool home, from the browser. Runs as you inside your own Tidepool, so what you can
 * see and change here is exactly what your shell can. Main folders a Dive holds are locked (read-only
 * until it ends). Folders download as a .zip; drop files or folders anywhere on the list to upload.
 */
export function FilesPage() {
  const { logout } = useAuth();
  const [dir, setDir] = useState(".");
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [tp, setTp] = useState<Tidepool | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);
  const [making, setMaking] = useState(false);
  const [clashes, setClashes] = useState<Pending[]>([]);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);

  const fail = useCallback((e: unknown) => {
    if (e instanceof Unauthorized) logout();
    else setError(errMessage(e));
  }, [logout]);

  const load = useCallback(() => {
    listFiles(dir).then((r) => setEntries(r.entries)).catch(fail);
    getTidepool().then(setTp).catch(fail);
  }, [dir, fail]);
  useEffect(load, [load]);

  const go = (path: string) => { setEntries(null); setError(null); setRenaming(null); setDeleting(null); setDir(path); };

  const run = async (label: string, f: () => Promise<void>) => {
    setBusy(label); setError(null);
    try { await f(); load(); } catch (e) { fail(e); } finally { setBusy(null); }
  };

  const upload = (items: Pending[], overwrite = false) => run("upload", async () => {
    const exist: Pending[] = [];
    for (const [i, it] of items.entries()) {
      setBusy(`uploading ${i + 1}/${items.length}: ${it.path}`);
      if ((await uploadFile(join(dir, it.path), it.file, overwrite)) === "exists") exist.push(it);
    }
    setClashes(exist);
  });

  const picked = (files: FileList | null) => {
    if (!files?.length) return;
    upload(Array.from(files).map((f) => ({ path: f.webkitRelativePath || f.name, file: f })));
  };

  const dropped = async (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const roots = Array.from(e.dataTransfer.items).map((i) => i.webkitGetAsEntry()).filter(Boolean);
    upload((await Promise.all(roots.map((r) => walk(r as FileSystemEntry, "")))).flat());
  };

  const crumbs = dir === "." ? [] : dir.split("/");
  const usage = tp && tp.size_gb ? `${tp.used_gb.toFixed(1)} / ${tp.size_gb} GB` : "—";
  const pct = tp && tp.size_gb ? Math.min(100, (tp.used_gb / tp.size_gb) * 100) : 0;

  return (
    <Page
      title="Files"
      actions={
        <span className="flex items-center gap-2">
          <button className="btn-ghost" disabled={busy !== null} onClick={() => setMaking(true)}>New folder</button>
          <button className="btn-ghost" disabled={busy !== null} onClick={() => fileInput.current?.click()}>Upload files</button>
          <button className="btn-ghost" disabled={busy !== null} onClick={() => folderInput.current?.click()}>Upload a folder</button>
          <input ref={fileInput} type="file" multiple hidden onChange={(e) => { picked(e.target.files); e.target.value = ""; }} />
          <input ref={folderInput} type="file" hidden
            {...({ webkitdirectory: "" } as Record<string, string>)}
            onChange={(e) => { picked(e.target.files); e.target.value = ""; }} />
        </span>
      }
    >
      <div className="mb-5 max-w-md">
        <div className="flex justify-between text-[13px] text-ink-soft"><span>your Tidepool</span><span className="num">{usage}</span></div>
        <div className="mt-1 h-1.5 rounded-full" style={{ background: "rgba(255,255,255,0.08)" }}>
          <div className="h-1.5 rounded-full" style={{ width: `${pct}%`, background: pct > 90 ? "var(--alarm)" : "var(--irid-mid)" }} />
        </div>
      </div>

      <nav className="mb-4 flex flex-wrap items-center gap-1 text-[14px]">
        <button className="text-ink-soft hover:text-ink" onClick={() => go(".")}>~</button>
        {crumbs.map((c, i) => (
          <span key={i} className="flex items-center gap-1">
            <span className="text-ink-mute">/</span>
            <button className="text-ink-soft hover:text-ink" onClick={() => go(crumbs.slice(0, i + 1).join("/"))}>{c}</button>
          </span>
        ))}
      </nav>

      {error && <p className="mb-4 text-[13px]" style={{ color: "var(--alarm)" }}>{error}</p>}
      {busy && <p className="mb-4 text-[13px] text-ink-soft">{busy}…</p>}

      {clashes.length > 0 && (
        <div className="surface mb-4 space-y-2" style={{ padding: "calc(var(--pad) + 6px)" }}>
          <p className="text-[13px] text-ink-soft">
            {clashes.length} already there: {clashes.slice(0, 5).map((c) => c.path).join(", ")}
            {clashes.length > 5 ? ", …" : ""}
          </p>
          <span className="flex gap-2">
            <button className="btn-primary" onClick={() => upload(clashes, true)}>Overwrite them</button>
            <button className="btn-ghost" onClick={() => setClashes([])}>Skip them</button>
          </span>
        </div>
      )}

      {making && (
        <form className="mb-4 flex gap-2" onSubmit={(e) => {
          e.preventDefault();
          const n = newName.trim();
          setMaking(false); setNewName("");
          if (n) run("new folder", () => makeFolder(join(dir, n)));
        }}>
          <input autoFocus className="field" placeholder="folder name" value={newName} onChange={(e) => setNewName(e.target.value)} />
          <button className="btn-primary" type="submit">Create</button>
          <button className="btn-ghost" type="button" onClick={() => setMaking(false)}>Cancel</button>
        </form>
      )}

      <div
        className="surface"
        style={{ padding: "calc(var(--pad) + 14px)", outline: dragging ? "1px dashed var(--irid-mid)" : undefined }}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={dropped}
      >
        <div className="label grid items-center gap-3 px-5 pb-4" style={{ gridTemplateColumns: COLS }}>
          <span>Name</span>
          <span className="text-right">Size</span>
          <span>Modified</span>
          <span />
        </div>
        {entries === null ? (
          <p className="px-5 py-6 text-[14px] text-ink-mute">Loading…</p>
        ) : entries.length === 0 ? (
          <p className="px-5 py-6 text-[14px] text-ink-mute">Empty — drop files or folders here to upload.</p>
        ) : (
          <ul>
            {entries.map((f) => (
              <li key={f.path} className="grid items-center gap-3 rounded-[10px] px-5 py-2.5" style={{ gridTemplateColumns: COLS }}>
                <span className="flex min-w-0 items-center gap-2">
                  {renaming === f.path ? (
                    <form className="flex gap-2" onSubmit={(e) => {
                      e.preventDefault();
                      const n = newName.trim();
                      setRenaming(null);
                      if (n && n !== f.name) run("rename", () => renameFile(f.path, join(dir, n)));
                    }}>
                      <input autoFocus className="field" value={newName} onChange={(e) => setNewName(e.target.value)} />
                      <button className="btn-ghost" type="submit">Save</button>
                    </form>
                  ) : f.type === "dir" ? (
                    <button className="truncate text-left text-[14px] text-ink" onClick={() => go(f.path)}>{f.name}/</button>
                  ) : (
                    <span className="truncate text-[14px] text-ink-soft">{f.name}{f.type === "link" ? " →" : ""}</span>
                  )}
                  {f.locked && (
                    <Tip className="text-[12px]" text="A running Dive holds this main folder: read-only here until it ends (its changes mirror back every minute).">
                      <span style={{ color: "var(--irid-mid)" }}>locked</span>
                    </Tip>
                  )}
                </span>
                <span className="num text-right text-[13px] text-ink-mute">{fmtSize(f.size)}</span>
                <span className="num text-[13px] text-ink-mute">{fmtTime(f.mtime)}</span>
                <span className="flex justify-end gap-2 text-[13px]">
                  {deleting === f.path ? (
                    <>
                      <button className="btn-primary" onClick={() => { setDeleting(null); run("delete", () => deleteFile(f.path)); }}>Delete</button>
                      <button className="btn-ghost" onClick={() => setDeleting(null)}>Keep</button>
                    </>
                  ) : (
                    <>
                      <a className="btn-ghost" href={downloadUrl(f.path)} download>{f.type === "dir" ? ".zip" : "Download"}</a>
                      {!f.locked && (
                        <>
                          <button className="btn-ghost" onClick={() => { setRenaming(f.path); setNewName(f.name); }}>Rename</button>
                          <button className="btn-ghost" onClick={() => setDeleting(f.path)}>Delete</button>
                        </>
                      )}
                    </>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Page>
  );
}
