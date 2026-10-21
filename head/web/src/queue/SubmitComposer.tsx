import type { Arm } from "../api";
import { Composer } from "../app/Composer";

/** Submit a Spawn — a thin wrapper over app/Composer.tsx. */
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
  return <Composer arms={arms} isAdmin={isAdmin} startOpen={startOpen} onSubmitted={onSubmitted} />;
}
