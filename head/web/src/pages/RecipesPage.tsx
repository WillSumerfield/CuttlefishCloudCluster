import { useCallback, useEffect, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { getRecipes, createRecipe, runRecipe, updateRecipe, deleteRecipe, Unauthorized, errMessage, type Recipe } from "../api";
import { Storage } from "../recipes/Storage";
import { RecipeCard } from "../recipes/RecipeCard";
import { RecipeComposer, type RecipePayload } from "../recipes/RecipeComposer";
import { NewRecipeTile } from "../recipes/NewRecipeTile";
import { demoRecipes } from "../recipes/mock";
import { isDemo } from "../queue/mock";
import type { RecipeAction } from "../recipes/format";

/**
 * Recipes — your shelf of frozen, re-runnable snapshots. Private, so there is no roster: the whole page is
 * yours. The storage wall sits on top (the signature), the Recipes below it, each linked to the slice it
 * fills on the wall. The first cell of the grid is the way ON: a ghost tile that hands the shelf over to the
 * composer, which replaces the grid while you freeze a folder (the wall stays, previewing the new bytes).
 *
 * Update code re-opens the composer rather than acting in place, because an update IS a fresh upload —
 * there is no copy of the folder on the Head to re-freeze, and none on your machine the Head can reach.
 * So it prefills the Recipe's settings, fixes its name, and hands the wall an allowance the Recipe's own
 * bytes have already come off: on the Head an update is delete-then-save, charged what it grows by.
 *
 * Under ?demo everything plays out on the mock in place: Save appends a card that blooms while it
 * "uploads" then re-freezes; Delete drops it and frees its bytes; Update code blooms for a beat then
 * re-freezes. Live, they call out and a refetch confirms — except Run, which the Head answers 501 to.
 */

const API: Record<"run" | "delete", (name: string) => Promise<void>> = {
  run: runRecipe, delete: deleteRecipe,
};

export function RecipesPage() {
  const { logout } = useAuth();
  const demo = isDemo();
  const [recipes, setRecipes] = useState<Recipe[]>(() => (demo ? demoRecipes() : []));
  const [loaded, setLoaded] = useState(demo);
  const [hovered, setHovered] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);
  // The Recipe being re-captured, or null for a new one. Same composer either way — an update differs
  // only in what it prefills and which endpoint takes the upload.
  const [editing, setEditing] = useState<Recipe | null>(null);
  const [pending, setPending] = useState(0); // bytes of the folder being frozen right now → wall ghost
  // A failed action/save used to vanish silently — worst of all for Save, where a folder was picked and a
  // form filled. Name what failed until the next action.
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRecipes(await getRecipes());
    } catch (e) {
      if (e instanceof Unauthorized) logout();
    } finally {
      setLoaded(true);
    }
  }, [logout]);

  useEffect(() => {
    if (demo) return; // design harness — hold the mock, don't fetch. Recipes are frozen; no polling needed.
    refresh();
  }, [demo, refresh]);

  const onAct = useCallback(
    (name: string, action: RecipeAction) => {
      if (action === "edit") return; // settings form — a later phase; the control is here, inert for now.

      if (action === "update") {
        // An update needs a folder, so it goes through the composer rather than firing an endpoint —
        // nothing moves here, and nothing is charged, until a folder is picked and Update code is hit.
        const target = recipes.find((r) => r.name === name);
        if (!target) return;
        setEditing(target);
        setComposing(true);
        setActionError(null);
        return;
      }

      if (action === "delete") {
        setRecipes((prev) => prev.filter((r) => r.name !== name));
      }
      // Run reshapes nothing here — it spawns a job that surfaces on the Queue, not on this page.

      setActionError(null);
      if (demo) return;
      API[action as "run" | "delete"](name).catch((e) => {
        if (e instanceof Unauthorized) return logout();
        setActionError(`Couldn't ${action} recipe '${name}' — ${errMessage(e)} The shelf has been refreshed.`);
        refresh(); // an action that failed live: re-sync to the truth.
      });
    },
    [demo, logout, recipes, refresh],
  );

  // Save a Recipe, new or re-captured: collapse to the grid at once, show a card that BLOOMS while its tree
  // uploads, then settles to "frozen just now". Under ?demo the settle is a timer; live, the Head's answer
  // replaces the optimistic card (and carries its true size_bytes).
  //
  // A new save appends; an update replaces in place, so the shelf never briefly shows two of one Recipe.
  const onSave = useCallback(
    (r: RecipePayload) => {
      const target = editing;
      const optimistic: Recipe = {
        name: r.name,
        script: r.script,
        args: r.args,
        outputs: r.outputs,
        gpus: r.gpus,
        vram_mb: r.vram_mb,
        minutes: r.minutes,
        size_bytes: r.picked.bytes,
        frozen_at: Date.now(),
        updating: true,
        running: false,
        wandb: r.wandb,
        wandb_project: r.wandb_project,
        wandb_entity: r.wandb_entity,
      };
      setRecipes((prev) =>
        target ? prev.map((x) => (x.name === target.name ? optimistic : x)) : [...prev, optimistic],
      );
      setComposing(false);
      setEditing(null);
      setPending(0);
      setActionError(null);

      if (demo) {
        window.setTimeout(() => {
          setRecipes((prev) =>
            prev.map((x) => (x.name === r.name ? { ...x, updating: false, frozen_at: Date.now() } : x)),
          );
        }, 2600);
        return;
      }
      const sent = target ? updateRecipe(target.name, r) : createRecipe(r);
      sent
        .then((saved) => setRecipes((prev) => prev.map((x) => (x.name === r.name ? saved : x))))
        .catch((e) => {
          if (e instanceof Unauthorized) return logout();
          // Failed live: re-sync and say so. The refetch is what puts an updated Recipe's OLD snapshot
          // back on the shelf — the Head refuses before touching it, so it is still there and still runs.
          const what = target ? `update recipe '${target.name}'` : `save recipe '${r.name}'`;
          setActionError(`Couldn't ${what} — ${errMessage(e)} Nothing was changed.`);
          refresh();
        });
    },
    [demo, editing, logout, refresh],
  );

  const used = recipes.reduce((n, r) => n + r.size_bytes, 0);
  const empty = loaded && recipes.length === 0 && !composing;

  return (
    <Page
      title="Recipes"
      actions={
        loaded && recipes.length > 0 ? (
          <span className="text-[13px] text-ink-mute">
            <span className="num text-ink-soft">{recipes.length}</span> recipe{recipes.length === 1 ? "" : "s"}
          </span>
        ) : null
      }
    >
      {demo && (
        <p
          className="mb-6 rounded-[10px] px-3 py-2 text-[13px]"
          style={{ background: "color-mix(in oklab, var(--irid-low) 12%, transparent)", color: "var(--ink-soft)" }}
        >
          Demo data — not your real shelf. Drop the <span className="num">?demo</span> from the URL for your Recipes.
        </p>
      )}

      {actionError && (
        <p role="alert" className="mb-6 rounded-[10px] border px-3 py-2 text-[13px]"
           style={{ borderColor: "color-mix(in oklab, var(--alarm) 40%, transparent)", color: "var(--alarm)" }}>
          {actionError}
        </p>
      )}

      <div className="space-y-8">
        <Storage recipes={recipes} hovered={hovered} pending={pending} />

        {composing ? (
          <RecipeComposer
            // Delete-then-save: an update's own bytes come off before the new folder is weighed, so the
            // wall previews exactly the arithmetic recipes.save does on the Head. Its name comes off the
            // taken list for the same reason — a Recipe is not a duplicate of itself.
            used={used - (editing?.size_bytes ?? 0)}
            existingNames={recipes.map((r) => r.name).filter((n) => n !== editing?.name)}
            editing={editing ?? undefined}
            onPending={setPending}
            onSave={onSave}
            onCancel={() => {
              setComposing(false);
              setEditing(null);
              setPending(0);
            }}
          />
        ) : (
          <section>
            <h2 className="label mb-3 pl-1">My recipes</h2>
            {empty && (
              <p className="mb-3 px-1 text-[14px] text-ink-mute">
                Nothing frozen yet. Freeze a project into a Recipe — then run it again from anywhere, with
                nothing to hand.
              </p>
            )}
            <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <NewRecipeTile
                onClick={() => {
                  setEditing(null); // never inherit the last Update's prefill
                  setComposing(true);
                }}
              />
              {recipes.map((r) => (
                <RecipeCard key={r.name} recipe={r} onHover={setHovered} onAct={onAct} />
              ))}
            </ul>
          </section>
        )}
      </div>
    </Page>
  );
}
