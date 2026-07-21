import { you, members } from "../karma/mock";
import { RECIPE_ALLOWANCE_BYTES } from "../api";

/**
 * Demo persona for reviewing Account without a live cluster — loaded ONLY under `?demo`. It's the same
 * "you" the rest of the design harness wears (Nadia, +620 on the Karma Standings), fleshed out with the
 * facts this page states plainly: your Share, the Arms you own, the storage you hold, a W&B key already set.
 */

// Your Share of the standing: your Balance as a fraction of all the credit in the field. A debtor has none.
export function demoShare(): number {
  const credit = members.reduce((n, m) => n + Math.max(0, m.balance), 0);
  return credit > 0 ? Math.max(0, you.balance) / credit : 0;
}

export const demoAccount = {
  name: you.name,
  admin: true,
  balance: you.balance,
  arms: ["arm-cephalo-04 · RTX 4090", "arm-siphon-02 · A6000"],
  storageUsedBytes: 6.2 * 1024 ** 3,
  allowanceBytes: RECIPE_ALLOWANCE_BYTES,
  // A W&B key already stored, so the module shows the masked + reveal state rather than the empty one.
  wandbKey: "wandb_a1b2c3d4e5f6a7b8c9d0e1f23f9a",
};
