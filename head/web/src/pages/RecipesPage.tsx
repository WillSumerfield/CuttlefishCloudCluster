import { Page } from "../app/Page";
import { Placeholder } from "./Placeholder";

/** Frozen, re-runnable snapshots of a project. Private to their owner. */
export function RecipesPage() {
  return (
    <Page title="Recipes">
      <Placeholder what="Runs you have frozen: the code and how to run it, ready to fire from any machine with nothing to hand." />
    </Page>
  );
}
