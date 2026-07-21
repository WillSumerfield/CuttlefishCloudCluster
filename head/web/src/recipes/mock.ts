import type { Recipe } from "../api";

/**
 * Demo data for reviewing Recipes without a live cluster — loaded ONLY under `?demo`. Recipes are private,
 * so this is one User's shelf, not a roster: a spread of sizes (to fill the storage wall to ~16 of 20 GB),
 * a spread of staleness (fresh to weeks old), and ONE mid-update so the bloom is on screen next to the
 * still, frozen rest.
 */

const GB = 1024 ** 3;
const MB = 1024 ** 2;

export function demoRecipes(): Recipe[] {
  const now = Date.now();
  const h = 3600_000;
  const d = 24 * h;
  const R = (o: Partial<Recipe> & { name: string }): Recipe => ({
    script: "train.py", args: [], gpus: 1, vram_mb: 24576, minutes: 120, size_bytes: GB, frozen_at: now, updating: false, running: false, ...o,
  });
  return [
    R({ name: "grpo-sweep", script: "train.py", args: ["--config", "grpo.yaml"], gpus: 4, vram_mb: 81920, minutes: 720, size_bytes: 6.2 * GB, frozen_at: now - 5 * d, running: true }),
    R({ name: "nightly-eval", script: "eval.py", args: ["--suite", "full"], gpus: 2, vram_mb: 40960, minutes: 180, size_bytes: 3.7 * GB, frozen_at: now - 26 * d, updating: true }),
    R({ name: "dpo-7b", script: "train_dpo.py", args: ["--beta", "0.1"], gpus: 8, vram_mb: 81920, minutes: 1440, size_bytes: 4.4 * GB, frozen_at: now - 12 * h }),
    R({ name: "tokenizer-bench", script: "bench.py", gpus: 1, vram_mb: 24576, minutes: 45, size_bytes: 1.1 * GB, frozen_at: now - 2 * h }),
    R({ name: "quick-probe", script: "probe.py", args: ["--n", "256"], gpus: 1, vram_mb: 16384, minutes: 20, size_bytes: 280 * MB, frozen_at: now - 40 * d }),
  ];
}
