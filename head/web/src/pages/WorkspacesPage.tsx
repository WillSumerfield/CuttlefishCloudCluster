import { useCallback, useEffect, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import {
  createWorkspace, deleteWorkspace, getArms, getWorkspaces, openWorkspace, updateWorkspace,
  Unauthorized, errMessage, type Arm, type Workspace,
} from "../api";
import { ComposerSlot } from "../workspaces/ComposerSlot";
import { Storage } from "../workspaces/Storage";
import { WorkspaceCard } from "../workspaces/WorkspaceCard";
import { NewWorkspaceTile } from "../workspaces/NewWorkspaceTile";
import {
  WorkspaceComposer, type WorkspacePayload, type WorkspaceSettingsPayload,
} from "../workspaces/WorkspaceComposer";
import { VisitPopup } from "../workspaces/VisitPopup";
import type { WorkspaceAction } from "../workspaces/format";

/**
 * Workspaces — the working directories the Head keeps. Sibling page to Recipes and deliberately the
 * same shape (meter on top, grid below, first cell is the way on, Edit turns a card into the composer
 * in place), because they are siblings: a Recipe is frozen and re-run in batch, a Workspace is mutable
 * and visited.
 *
 * Two things differ, and both come from a Workspace being ALIVE where a Recipe is not:
 *
 * 1. **It polls.** A Recipe only changes when its owner changes it, so that page fetches once. A
 *    Workspace's tree changes from inside a live session, and `used_bytes` is measured off the disk on
 *    every read — so while any visit is live this refetches, and stops when none is.
 * 2. **Open is not optimistic.** Delete still is (the card leaves before the Head confirms), but a
 *    visit's id comes from sbatch and cannot be guessed — so Open waits for the Head's answer and then
 *    says what to do with it, since the Dashboard has no browser terminal to attach from.
 */
export function WorkspacesPage() {
  const { logout } = useAuth();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [arms, setArms] = useState<Arm[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const [active, setActive] = useState<"new" | string | null>(null);
  const [pending, setPending] = useState(0);   // the Storage the composer is about to reserve → meter ghost
  const [visit, setVisit] = useState<{ name: string; id: number; attached: boolean } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setWorkspaces(await getWorkspaces());
    } catch (e) {
      if (e instanceof Unauthorized) logout();
    } finally {
      setLoaded(true);
    }
  }, [logout]);

  useEffect(() => {
    refresh();
    getArms().then(setArms).catch(() => setArms([]));
  }, [refresh]);

  // Only while something is live: an idle shelf is as static as the Recipe one, and polling it would be
  // a request every few seconds for an answer that cannot have changed.
  const anyLive = workspaces.some((w) => w.live || w.starting);
  useEffect(() => {
    if (!anyLive) return;
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [anyLive, refresh]);

  const onAct = useCallback(
    (name: string, action: WorkspaceAction) => {
      setActionError(null);

      if (action === "edit") {
        if (active) return;   // one composer open at a time, like the Recipe shelf
        setActive(name);
        return;
      }
      if (action === "download") return;   // a plain link on the card; nothing to do here

      if (action === "delete") {
        setWorkspaces((prev) => prev.filter((w) => w.name !== name));
        deleteWorkspace(name).catch((e) => {
          if (e instanceof Unauthorized) return logout();
          setActionError(`Couldn't delete workspace '${name}' — ${errMessage(e)} The shelf has been refreshed.`);
          refresh();
        });
        return;
      }

      // Open. Not optimistic: the visit's Spawn id is sbatch's answer and there is nothing to guess.
      openWorkspace(name)
        .then((r) => {
          setVisit({ name, id: r.id, attached: r.attached });
          refresh();   // the card's own live/starting state is the Head's to report
        })
        .catch((e) => {
          if (e instanceof Unauthorized) return logout();
          setActionError(`Couldn't open workspace '${name}' — ${errMessage(e)}`);
          refresh();
        });
    },
    [active, logout, refresh],
  );

  // Create. Optimistic like a Recipe save, with `starting` standing in for the bloom while the seed
  // uploads — the one moment a Workspace is being written to from outside a session.
  const onSave = useCallback(
    (w: WorkspacePayload) => {
      const optimistic: Workspace = {
        name: w.name,
        storage_bytes: w.storage_bytes,
        used_bytes: w.picked.bytes,
        gpus: w.gpus,
        vram_mb: w.vram_mb,
        minutes: w.minutes,
        manifest: w.manifest,
        substrate: w.substrate,
        outputs: w.outputs,
        features: w.features,
        created_at: Date.now(),
        last_visited_at: null,
        live: null,
        starting: true,
      };
      setWorkspaces((prev) => [...prev, optimistic]);
      setActive(null);
      setPending(0);

      createWorkspace(w)
        .then((saved) => setWorkspaces((prev) => prev.map((x) => (x.name === w.name ? saved : x))))
        .catch((e) => {
          if (e instanceof Unauthorized) return logout();
          setActionError(`Couldn't create workspace '${w.name}' — ${errMessage(e)} Nothing was created.`);
          refresh();
        });
    },
    [logout, refresh],
  );

  // Edit: settings and Storage, never files. Optimistic, but `used_bytes` is deliberately NOT touched —
  // it is a disk measurement, and guessing it here would briefly draw a fill nobody measured.
  const onSaveMeta = useCallback(
    (name: string, w: WorkspaceSettingsPayload) => {
      setWorkspaces((prev) => prev.map((x) => (x.name === name ? { ...x, ...w } : x)));
      setActive(null);
      setPending(0);

      updateWorkspace(name, w)
        .then((saved) => setWorkspaces((prev) => prev.map((x) => (x.name === name ? saved : x))))
        .catch((e) => {
          if (e instanceof Unauthorized) return logout();
          setActionError(`Couldn't update workspace '${name}' — ${errMessage(e)} Nothing was changed.`);
          refresh();
        });
    },
    [logout, refresh],
  );

  const reserved = workspaces.reduce((n, w) => n + w.storage_bytes, 0);
  const empty = loaded && workspaces.length === 0 && active !== "new";

  return (
    <Page
      title="Workspaces"
      actions={
        loaded && workspaces.length > 0 ? (
          <span className="text-[13px] text-ink-mute">
            <span className="num text-ink-soft">{workspaces.length}</span> workspace
            {workspaces.length === 1 ? "" : "s"}
          </span>
        ) : null
      }
    >
      {visit && <VisitPopup {...visit} onClose={() => setVisit(null)} />}

      {actionError && (
        <p role="alert" className="mb-6 rounded-[10px] border px-3 py-2 text-[13px]"
           style={{ borderColor: "color-mix(in oklab, var(--alarm) 40%, transparent)", color: "var(--alarm)" }}>
          {actionError}
        </p>
      )}

      <div className="space-y-8">
        <Storage workspaces={workspaces} hovered={hovered} pending={pending} />

        <section>
          <h2 className="label mb-3 pl-1">My workspaces</h2>
          {empty && (
            <p className="mb-3 px-1 text-[14px] text-ink-mute">
              Nothing here yet. A workspace is a folder the cluster keeps — open it, work in it, and
              everything you leave behind is still there next time.
            </p>
          )}
          <ul className="grid items-start gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <ComposerSlot
              isOpen={active === "new"}
              closed={<NewWorkspaceTile onClick={() => !active && setActive("new")} />}
              closedTint="color-mix(in oklab, var(--abyss) 75%, var(--shoal))"
              open={
                <WorkspaceComposer
                  arms={arms}
                  reserved={reserved}
                  existingNames={workspaces.map((w) => w.name)}
                  onPending={setPending}
                  onSave={onSave}
                  onCancel={() => { setActive(null); setPending(0); }}
                />
              }
            />
            {workspaces.map((w) => (
              <ComposerSlot
                key={w.name}
                isOpen={active === w.name}
                closed={<WorkspaceCard workspace={w} onHover={setHovered} onAct={onAct} />}
                open={
                  // This Workspace's own reservation comes off the pool first, so the meter previews
                  // exactly the delta arithmetic workspaces.update_meta does on the Head.
                  <WorkspaceComposer
                    arms={arms}
                    reserved={reserved - w.storage_bytes}
                    existingNames={workspaces.map((x) => x.name).filter((n) => n !== w.name)}
                    editing={w}
                    onPending={setPending}
                    onSave={onSave}
                    onSaveMeta={(patch) => onSaveMeta(w.name, patch)}
                    onCancel={() => { setActive(null); setPending(0); }}
                  />
                }
              />
            ))}
          </ul>
        </section>
      </div>
    </Page>
  );
}
