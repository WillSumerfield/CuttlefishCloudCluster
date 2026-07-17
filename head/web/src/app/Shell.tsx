import { AbyssBackground } from "./AbyssBackground";
import { AuthGate } from "./auth";
import { Rail } from "./Rail";
import { spawnIdFrom, usePath } from "./router";
import { session } from "./session";

import App from "../App";
import { KarmaPage } from "../karma/KarmaPage";
import { QueuePage } from "../pages/QueuePage";
import { HistoryPage } from "../pages/HistoryPage";
import { ArmsPage } from "../pages/ArmsPage";
import { RecipesPage } from "../pages/RecipesPage";
import { TidePage } from "../pages/TidePage";
import { AccountPage } from "../pages/AccountPage";
import { UsersPage } from "../pages/UsersPage";
import { SpawnPage } from "../pages/SpawnPage";
import { NotFoundPage } from "../pages/NotFoundPage";

/**
 * The shell. The shader is mounted HERE, above the routing switch, so it survives every navigation —
 * the water is one continuous body and the pages float on it. Remounting it per page would restart the
 * simulation on every click, and the seam would be the most visible thing on the site.
 */

function Route() {
  const path = usePath();

  const spawnId = spawnIdFrom(path);
  if (spawnId) return <SpawnPage id={spawnId} />;

  switch (path) {
    case "/":
      return <QueuePage />;
    case "/history":
      return <HistoryPage />;
    case "/arms":
      return <ArmsPage />;
    case "/recipes":
      return <RecipesPage />;
    case "/karma":
      return <KarmaPage />;
    case "/tide":
      return <TidePage />;
    case "/account":
      return <AccountPage />;
    // Not a redirect: a non-admin who types /users should be told it isn't there, not bounced somewhere
    // they didn't ask for.
    case "/users":
      return session.isAdmin ? <UsersPage /> : <NotFoundPage />;
    default:
      return <NotFoundPage />;
  }
}

export function Shell() {
  const path = usePath();

  // The old dashboard is fully self-contained — its own sign-in, its own polling — and is the only
  // thing that can submit a job today. It stays reachable at /legacy, unlinked and OUTSIDE the new
  // auth gate, until the new pages replace what it does. Delete this and App.tsx together, not before.
  if (path === "/legacy") return <App />;

  return (
    <>
      <AbyssBackground />
      <AuthGate>
        <Rail />
        {/* pl-52 == the rail's w-52. On mobile the rail is a bottom bar and takes no width. */}
        <div className="md:pl-52">
          <Route />
        </div>
      </AuthGate>
    </>
  );
}
