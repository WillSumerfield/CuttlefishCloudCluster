/**
 * Where each Dashboard page is explained in the wiki (repo `wiki/`, served by the Head at /wiki/).
 * Quartz slugs: spaces become dashes, no `.html`. The wiki is a separate site, so these are plain links,
 * not client-side routes. `check_links` in the wiki does not see these — keep them in step by hand.
 */
export const WIKI = "/wiki/";

const HELP: [prefix: string, slug: string][] = [
  ["/dive/", "Using-CCC/Dives"],
  ["/history", "Using-CCC/Dives"],
  ["/arms", "Donating/Arms"],
  ["/karma", "Using-CCC/Karma"],
  ["/tide", "Using-CCC/Karma"],
  ["/files", "Using-CCC/Files"],
  ["/account", "Using-CCC/Tidepool"],
  ["/users", "Admin/Users-and-access"],
];

/** The wiki page for a Dashboard path; Queue (`/`) and anything unlisted get the Dashboard tour. */
export function wikiFor(path: string): string {
  const hit = HELP.find(([p]) => path === p || path.startsWith(p.endsWith("/") ? p : p + "/"));
  return WIKI + (hit ? hit[1] : "Using-CCC/Dashboard-tour");
}
