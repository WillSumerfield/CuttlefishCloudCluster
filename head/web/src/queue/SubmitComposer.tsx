import type { Arm } from "../api";
import { Composer } from "../app/Composer";

/** Submit a Spawn — a thin wrapper over app/Composer.tsx. */
export function SubmitComposer({
  arms,
  isAdmin,
  onSubmitted,
}: {
  arms: Arm[];
  isAdmin: boolean;
  onSubmitted: () => void;
}) {
  return <Composer arms={arms} isAdmin={isAdmin} onSubmitted={onSubmitted} />;
}
