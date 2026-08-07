import type { Arm } from "../api";
import { Composer } from "../app/Composer";

/**
 * Submit a Spawn — the "spawn" variant of the shared composer (app/Composer.tsx). See its module
 * docstring for what actually differs between this and the Recipe composer, and why.
 */
export function SubmitComposer({
  arms,
  isAdmin,
  startOpen,
  onSubmitted,
}: {
  arms: Arm[];
  isAdmin: boolean;
  /** Open on load when the queue is empty — an empty screen is an invitation to act. */
  startOpen?: boolean;
  onSubmitted: () => void;
}) {
  return <Composer variant="spawn" arms={arms} isAdmin={isAdmin} startOpen={startOpen} onSubmitted={onSubmitted} />;
}
