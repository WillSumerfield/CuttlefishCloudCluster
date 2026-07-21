import type { ArchivedSpawn } from "../api";
import { session } from "../app/session";

/**
 * Demo data for reviewing History's design without a live cluster — the archive equivalent of
 * queue/mock.ts. Loaded ONLY under ?demo, never a silent fallback, so real emptiness reads as real.
 *
 * There is no live /history endpoint yet, so this is the only way to see the page populated. A spread of
 * outcomes on purpose: the page's whole job is to make done recede and failed/retracted stand out.
 */

const MIN = 60_000;

export function demoArchive(): ArchivedSpawn[] {
  const me = session.username || "you";
  const now = Date.now();
  // ago(m) — finished m minutes before now; the list is one flat newest-first stream.
  const ago = (m: number) => now - m * MIN;

  const rows: Array<Omit<ArchivedSpawn, "state" | "time_limit">> = [
    { id: 4821, name: "train.py", node: "arm-cephalo-04", gpus: 2, submitted_by: me, outcome: "done", exit: 0, held: 214, cost: 42, finished_at: ago(8) },
    { id: 4820, name: "sweep-λ", node: "arm-mantle-01", gpus: 1, submitted_by: "Ari Solberg", outcome: "failed", exit: 1, held: 3, cost: 6, finished_at: ago(31) },
    { id: 4819, name: "finetune-v7", node: "arm-siphon-02", gpus: 4, submitted_by: "Mira Kwan", outcome: "done", exit: 0, held: 118, cost: 96, finished_at: ago(52) },
    { id: 4815, name: "eval-batch", node: "arm-cephalo-04", gpus: 1, submitted_by: "Junho Park", outcome: "cancelled", exit: null, held: 6, cost: 3, finished_at: ago(74) },
    { id: 4812, name: "ablation-grid", node: "arm-siphon-02", gpus: 8, submitted_by: "Ari Solberg", outcome: "retracted", exit: null, held: 21, cost: 0, finished_at: ago(96) },
    { id: 4809, name: "probe", node: "arm-mantle-01", gpus: 1, submitted_by: me, outcome: "done", exit: 0, held: 38, cost: 9, finished_at: ago(140) },
    { id: 4804, name: "dpo-run", node: "arm-cephalo-04", gpus: 4, submitted_by: "Mira Kwan", outcome: "failed", exit: 139, held: 47, cost: 40, finished_at: ago(180) },
    { id: 4801, name: "tokenize", node: "arm-mantle-01", gpus: 1, submitted_by: "Junho Park", outcome: "done", exit: 0, held: 12, cost: 3, finished_at: ago(205) },
    { id: 4798, name: "grpo-7b", node: "arm-siphon-02", gpus: 8, submitted_by: me, outcome: "cancelled", exit: null, held: 52, cost: 34, finished_at: ago(250) },
    { id: 4793, name: "eval-mmlu", node: "arm-cephalo-04", gpus: 2, submitted_by: "Ari Solberg", outcome: "done", exit: 0, held: 88, cost: 36, finished_at: ago(320) },
    { id: 4790, name: "sweep-lr", node: "arm-mantle-01", gpus: 1, submitted_by: "Mira Kwan", outcome: "failed", exit: 1, held: 1, cost: 1, finished_at: ago(410) },
    { id: 4787, name: "distill", node: "arm-siphon-02", gpus: 4, submitted_by: "Junho Park", outcome: "retracted", exit: null, held: 9, cost: 0, finished_at: ago(500) },
    { id: 4783, name: "pretrain-c4", node: "arm-cephalo-04", gpus: 8, submitted_by: me, outcome: "done", exit: 0, held: 356, cost: 288, finished_at: ago(720) },
    { id: 4779, name: "bench-vllm", node: "arm-mantle-01", gpus: 2, submitted_by: "Ari Solberg", outcome: "done", exit: 0, held: 24, cost: 10, finished_at: ago(900) },
    { id: 4771, name: "sae-train", node: "arm-siphon-02", gpus: 1, submitted_by: "Junho Park", outcome: "cancelled", exit: null, held: 15, cost: 8, finished_at: ago(1180) },
    { id: 4762, name: "rlhf-step2", node: "arm-cephalo-04", gpus: 4, submitted_by: "Mira Kwan", outcome: "failed", exit: 137, held: 62, cost: 52, finished_at: ago(1500) },
  ];

  // state/time_limit are live-Spawn fields History never shows; fill them so the type is satisfied.
  return rows.map((r) => ({ ...r, state: r.outcome, time_limit: null }));
}
