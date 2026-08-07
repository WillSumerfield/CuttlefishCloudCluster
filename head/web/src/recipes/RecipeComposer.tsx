import type { Recipe } from "../api";
import { Composer, type RecipePayload, type RecipeSettingsPayload } from "../app/Composer";

export type { RecipePayload, RecipeSettingsPayload };

/**
 * Freeze a project into a Recipe — the "recipe" variant of the shared composer (app/Composer.tsx). See
 * its module docstring for what actually differs between this and the Spawn composer, and why.
 */
export function RecipeComposer({
  used,
  existingNames,
  editing,
  onPending,
  onSave,
  onSaveMeta,
  onCancel,
}: {
  /** Bytes already committed to the wall — the fit test adds the picked folder to this. */
  used: number;
  /** Names already taken on this shelf; a Recipe's name is its key, so it must be unique. */
  existingNames: string[];
  /** The Recipe being re-captured, or undefined for a new one. A folder is optional once editing — drop
   *  one to also refresh the code, or leave it and Save changes settings alone. */
  editing?: Recipe;
  /** Report the picked folder's bytes up so the page can preview them as a ghost on the wall. */
  onPending: (bytes: number) => void;
  onSave: (r: RecipePayload) => void;
  /** Required whenever `editing` is set — the no-folder-dropped save path. */
  onSaveMeta?: (r: RecipeSettingsPayload) => void;
  onCancel: () => void;
}) {
  return (
    <Composer
      variant="recipe"
      used={used}
      existingNames={existingNames}
      editing={editing}
      onPending={onPending}
      onSave={onSave}
      onSaveMeta={onSaveMeta}
      onCancel={onCancel}
    />
  );
}
