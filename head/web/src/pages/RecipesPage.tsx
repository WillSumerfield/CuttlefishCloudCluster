import { useCallback, useEffect, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { getRecipes, createRecipe, runRecipe, updateRecipe, deleteRecipe, Unauthorized, type Recipe } from "../api";
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
 * The action endpoints don't exist on the Head yet, so under ?demo everything plays out on the mock in
 * place: Save appends a card that blooms while it "uploads" then re-freezes; Delete drops it and frees its
 * bytes; Update code blooms for a beat then re-freezes. Live, they call out and a refetch confirms.
 */

const API: Record<"run" | "update" | "delete", (name: string) => Promise<void>> = {
  run: runRecipe, update: updateRecipe, delete: deleteRecipe,
};

export function RecipesPage() {
  const { logout } = useAuth();
  const demo = isDemo();
  const [recipes, setRecipes] = useState<Recipe[]>(() => (demo ? demoRecipes() : []));
  const [loaded, setLoaded] = useState(demo);
  const [hovered, setHovered] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);
  const [pending, setPending] = useState(0); // bytes of the folder being frozen right now → wall ghost

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

      if (action === "delete") {
        setRecipes((prev) => prev.filter((r) => r.name !== name));
      } else if (action === "update") {
        // Show the bloom, then re-freeze the snapshot at "now". Under ?demo this timer IS the upload; live,
        // the refetch below tells the truth and this optimistic beat is just the immediate feedback.
        setRecipes((prev) => prev.map((r) => (r.name === name ? { ...r, updating: true } : r)));
        if (demo) {
          window.setTimeout(() => {
            setRecipes((prev) =>
              prev.map((r) => (r.name === name ? { ...r, updating: false, frozen_at: Date.now() } : r)),
            );
          }, 2600);
        }
      }
      // Run reshapes nothing here — it spawns a job that surfaces on the Queue, not on this page.

      if (demo) return;
      API[action as "run" | "update" | "delete"](name).catch((e) => {
        if (e instanceof Unauthorized) logout();
        else refresh(); // an action that failed live: re-sync to the truth.
      });
    },
    [demo, logout, refresh],
  );

  // Save a new Recipe: collapse to the grid at once, drop in a card that BLOOMS while its tree uploads, then
  // settles to "frozen just now". Under ?demo the settle is a timer; live, createRecipe's answer replaces
  // the optimistic card (and carries the Head's true size_bytes).
  const onCreate = useCallback(
    (r: RecipePayload) => {
      const optimistic: Recipe = {
        name: r.name,
        script: r.script,
        args: r.args,
        gpus: r.gpus,
        vram_mb: r.vram_mb,
        minutes: r.minutes,
        size_bytes: r.picked.bytes,
        frozen_at: Date.now(),
        updating: true,
        running: false,
      };
      setRecipes((prev) => [...prev, optimistic]);
      setComposing(false);
      setPending(0);

      if (demo) {
        window.setTimeout(() => {
          setRecipes((prev) =>
            prev.map((x) => (x.name === r.name ? { ...x, updating: false, frozen_at: Date.now() } : x)),
          );
        }, 2600);
        return;
      }
      createRecipe(r)
        .then((saved) => setRecipes((prev) => prev.map((x) => (x.name === r.name ? saved : x))))
        .catch((e) => {
          if (e instanceof Unauthorized) logout();
          else refresh(); // the save failed live: drop the optimistic card, re-sync to the truth.
        });
    },
    [demo, logout, refresh],
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

      <div className="space-y-8">
        <Storage recipes={recipes} hovered={hovered} pending={pending} />

        {composing ? (
          <RecipeComposer
            used={used}
            existingNames={recipes.map((r) => r.name)}
            onPending={setPending}
            onSave={onCreate}
            onCancel={() => {
              setComposing(false);
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
              <NewRecipeTile onClick={() => setComposing(true)} />
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
