import { useCallback, useEffect, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { getArms, getRecipes, createRecipe, runRecipe, updateRecipe, updateRecipeSettings, deleteRecipe, Unauthorized, errMessage, type Arm, type Recipe } from "../api";
import { Storage } from "../recipes/Storage";
import { RecipeCard } from "../recipes/RecipeCard";
import { RecipeComposer, type RecipePayload, type RecipeSettingsPayload } from "../recipes/RecipeComposer";
import { NewRecipeTile } from "../recipes/NewRecipeTile";
import { ComposerSlot } from "../recipes/ComposerSlot";
import type { RecipeAction } from "../recipes/format";

/**
 * Recipes — your shelf of frozen, re-runnable snapshots. Private, no roster. Storage wall on top, Recipes
 * below, each linked to the slice it fills. First grid cell is the way ON: a ghost tile that, clicked,
 * becomes the composer in its own cell — row-spanning 2, pushing the column down rather than replacing
 * the shelf (wall previews the new bytes throughout).
 *
 * Edit turns THAT card into the same composer in place, prefilled and name fixed. A folder there is
 * optional: dropping one is a fresh upload (no copy exists on the Head or your machine to re-freeze
 * otherwise), weighed against the wall with the Recipe's own bytes already off (Head does delete-then-
 * save, charged what it grows by); leaving it out changes settings alone, no upload at all. Only one
 * composer open at a time.
 *
 * Delete is optimistic (the card leaves before the Head confirms, re-synced on failure). Run isn't:
 * nothing on THIS card changes locally — its `running` bloom is the Head's answer to the next fetch, not
 * a guess made here — so a successful run just triggers that refetch.
 */

const API: Record<"run" | "delete", (name: string) => Promise<unknown>> = {
  run: runRecipe, delete: deleteRecipe,
};

export function RecipesPage() {
  const { logout } = useAuth();
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  // The fleet, only so the composer can grey out GPU counts no Arm can host. Fetched once, not
  // polled — a Recipe is frozen, and an Arm joining mid-edit is the Head's to catch at save.
  const [arms, setArms] = useState<Arm[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  // Which grid slot is showing the composer instead of its normal tile: "new" for the New-recipe slot, a
  // Recipe's name for that card re-freezing in place, or null when nothing is open. Switching this swaps
  // a tile/card for a fresh RecipeComposer mount at that same slot — never two representations at once.
  const [active, setActive] = useState<"new" | string | null>(null);
  const [pending, setPending] = useState(0); // bytes of the folder being frozen right now → wall ghost
  // Failed actions are named rather than silent — worst case is Save, where a folder was picked and a
  // form filled. Cleared on the next action.
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
    refresh(); // Recipes are frozen; no polling needed.
    getArms().then(setArms).catch(() => setArms([])); // unreadable → every count stays pickable
  }, [refresh]);

  const onAct = useCallback(
    (name: string, action: RecipeAction) => {
      if (action === "edit") {
        // Doesn't fire an endpoint directly — opens the composer on this card instead. Whether the save
        // that follows also refreshes the code is the composer's own call (a dropped folder or not), not
        // this page's. Guarded like the New-recipe click below: only one composer open at a time.
        if (active) return;
        setActive(name);
        setActionError(null);
        return;
      }

      if (action === "delete") {
        setRecipes((prev) => prev.filter((r) => r.name !== name));
      }

      setActionError(null);
      const sent = API[action as "run" | "delete"](name);
      if (action === "run") sent.then(refresh); // the new Spawn is what flips `running` true
      sent.catch((e) => {
        if (e instanceof Unauthorized) return logout();
        setActionError(`Couldn't ${action} recipe '${name}' — ${errMessage(e)} The shelf has been refreshed.`);
        refresh(); // an action that failed live: re-sync to the truth.
      });
    },
    [active, logout, recipes, refresh],
  );

  // Save a Recipe, new or re-captured: collapse to a normal card at once, BLOOMING while its tree uploads,
  // then settling to "frozen just now" when the Head's answer replaces the optimistic card (and carries
  // its true size_bytes).
  //
  // A new save appends; an update replaces in place, so the shelf never briefly shows two of one Recipe.
  const onSave = useCallback(
    (r: RecipePayload) => {
      const target = active && active !== "new" ? recipes.find((x) => x.name === active) : undefined;
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
        manifest: r.manifest,
        features: r.features,
      };
      setRecipes((prev) =>
        target ? prev.map((x) => (x.name === target.name ? optimistic : x)) : [...prev, optimistic],
      );
      setActive(null);
      setPending(0);
      setActionError(null);

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
    [active, logout, recipes, refresh],
  );

  // Edit: settings only, no code, no bytes/frozen_at change — optimistic the same way onSave is, but
  // without the `updating` bloom (there's no upload for it to describe here).
  const onSaveMeta = useCallback(
    (name: string, r: RecipeSettingsPayload) => {
      const target = recipes.find((x) => x.name === name);
      if (!target) return;
      setRecipes((prev) =>
        prev.map((x) => (x.name === name ? { ...x, ...r, manifest: r.manifest } : x)),
      );
      setActive(null);
      setActionError(null);

      updateRecipeSettings(name, r)
        .then((saved) => setRecipes((prev) => prev.map((x) => (x.name === name ? saved : x))))
        .catch((e) => {
          if (e instanceof Unauthorized) return logout();
          setActionError(`Couldn't update recipe '${name}' — ${errMessage(e)} The shelf has been refreshed.`);
          refresh();
        });
    },
    [recipes, logout, refresh],
  );

  const used = recipes.reduce((n, r) => n + r.size_bytes, 0);
  const empty = loaded && recipes.length === 0 && active !== "new";

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
      {actionError && (
        <p role="alert" className="mb-6 rounded-[10px] border px-3 py-2 text-[13px]"
           style={{ borderColor: "color-mix(in oklab, var(--alarm) 40%, transparent)", color: "var(--alarm)" }}>
          {actionError}
        </p>
      )}

      <div className="space-y-8">
        <Storage recipes={recipes} hovered={hovered} pending={pending} />

        <section>
          <h2 className="label mb-3 pl-1">My recipes</h2>
          {empty && (
            <p className="mb-3 px-1 text-[14px] text-ink-mute">
              Nothing frozen yet. Freeze a project into a Recipe — then run it again from anywhere, with
              nothing to hand.
            </p>
          )}
          {/* items-start: a spanning composer inflates its OWN row track (native grid sizing), but without
              this every other card in that row (default align-items: stretch) would balloon to match —
              dead space stretched into an otherwise normal card. Top-aligning instead lets them sit at
              their natural height inside the tall row. */}
          <ul className="grid items-start gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <ComposerSlot
              isOpen={active === "new"}
              closed={<NewRecipeTile onClick={() => !active && setActive("new")} />}
              // Matches NewRecipeTile's own recess fill exactly — ComposerSlot's default tint is close
              // (both land in the same --abyss/--well family) but not identical, and this tile's own
              // fade already dissolves toward the real page background at its edges, so a mismatch here
              // would show as a seam right where the backdrop takes over during growing/shrinking.
              closedTint="color-mix(in oklab, var(--abyss) 75%, var(--shoal))"
              open={
                <RecipeComposer
                  arms={arms}
                  used={used}
                  existingNames={recipes.map((r) => r.name)}
                  onPending={setPending}
                  onSave={onSave}
                  onCancel={() => setActive(null)}
                />
              }
            />
            {recipes.map((r) => (
              <ComposerSlot
                key={r.name}
                isOpen={active === r.name}
                closed={<RecipeCard recipe={r} onHover={setHovered} onAct={onAct} />}
                open={
                  // Delete-then-save: an update's own bytes come off before the new folder is weighed, so
                  // the wall previews exactly the arithmetic recipes.save does on the Head. Its name comes
                  // off the taken list for the same reason — a Recipe is not a duplicate of itself.
                  <RecipeComposer
                    arms={arms}
                    used={used - r.size_bytes}
                    existingNames={recipes.map((x) => x.name).filter((n) => n !== r.name)}
                    editing={r}
                    onPending={setPending}
                    onSave={onSave}
                    onSaveMeta={(patch) => onSaveMeta(r.name, patch)}
                    onCancel={() => setActive(null)}
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
