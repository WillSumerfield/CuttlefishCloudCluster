import type { Arm, Spawn } from "../api";
import { session } from "../app/session";

/**
 * Demo data for reviewing the Queue's design without a live cluster. Loaded ONLY when the URL carries
 * `?demo` — never a silent fallback, so real emptiness always reads as real. A couple of Spawns are
 * attributed to the signed-in user so the cancel control is visible on them.
 *
 * Not shipped behavior: this is a design harness, the Queue's equivalent of karma/mock.ts.
 */

export function demoSpawns(): Spawn[] {
  const me = session.username || "you";
  return [
    { id: 4821, name: "train.py", state: "running", node: "arm-cephalo-04", gpus: 2, submitted_by: me, time_limit: 240 },
    { id: 4820, name: "sweep-λ", state: "running", node: "arm-mantle-01", gpus: 1, submitted_by: "Ari Solberg", time_limit: 360 },
    { id: 4819, name: "finetune-v7", state: "running", node: "arm-siphon-02", gpus: 4, submitted_by: "Mira Kwan", time_limit: 120 },
    { id: 4826, name: "eval-batch", state: "pending", node: null, gpus: 1, submitted_by: "Junho Park", time_limit: 40 },
    { id: 4827, name: "ablation-grid", state: "pending", node: null, gpus: 8, submitted_by: "Ari Solberg", time_limit: 600 },
    { id: 4828, name: "probe", state: "pending", node: null, gpus: 1, submitted_by: me, time_limit: 10 },
  ];
}

export function demoArms(): Arm[] {
  const arm = (name: string, gpus: number): Arm => ({
    name, state: "idle", gpus, owner: null, mesh_ip: null,
    gpu_device: null, admitted: true, gpu: null, vram_mb: null, power: null,
  });
  return [arm("arm-cephalo-04", 4), arm("arm-mantle-01", 8), arm("arm-siphon-02", 4)];
}

export const isDemo = () => new URLSearchParams(window.location.search).has("demo");
