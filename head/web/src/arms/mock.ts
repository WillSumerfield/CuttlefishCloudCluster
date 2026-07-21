import type { Arm } from "../api";
import { session } from "../app/session";

/**
 * Demo data for reviewing the Arms page without a live cluster — loaded ONLY under `?demo`, never a
 * silent fallback. Spread across every state the roster has to draw (allocated, idle, benchmarking, down,
 * awaiting-admit, awaiting-donor, retracted) and every owner shape (yours, someone else's, unowned). A
 * few are owned by the signed-in user so My Arms and its Retract / Return controls have something to show.
 */

export function demoRoster(): Arm[] {
  const me = session.username || "you";
  const A = (o: Partial<Arm> & { name: string }): Arm => ({
    state: "idle", gpus: 0, owner: null, mesh_ip: null, gpu_device: null,
    admitted: true, gpu: null, vram_mb: null, power: null, ...o,
  });
  return [
    A({ name: "arm-cephalo-04", state: "allocated", gpus: 4, owner: me, gpu: "H100", vram_mb: 81920, power: 989, mesh_ip: "100.72.0.4" }),
    A({ name: "arm-mantle-01", state: "allocated", gpus: 8, owner: "Mira Kwan", gpu: "A100", vram_mb: 81920, power: 312, mesh_ip: "100.72.0.1" }),
    A({ name: "arm-siphon-02", state: "idle", gpus: 4, owner: "Ari Solberg", gpu: "A100", vram_mb: 40960, power: 312, mesh_ip: "100.72.0.2" }),
    A({ name: "arm-fin-07", state: "benchmarking", gpus: 2, owner: me, gpu: "RTX 4090", vram_mb: 24576, power: null, mesh_ip: "100.72.0.7" }),
    A({ name: "arm-chroma-11", state: "idle", gpus: 1, owner: "Junho Park", gpu: "RTX 4090", vram_mb: 24576, power: 165, mesh_ip: "100.72.0.11" }),
    A({ name: "arm-ink-03", state: "down", gpus: 4, owner: "Mira Kwan", gpu: "A100", vram_mb: 40960, power: 312, mesh_ip: "100.72.0.3" }),
    A({ name: "arm-reef-09", state: "awaiting-admit", gpus: 8, owner: "Ari Solberg", gpu: "H100", vram_mb: 81920, power: 989, mesh_ip: "100.72.0.9", admitted: false }),
    A({ name: "arm-tide-12", state: "awaiting-donor", gpus: 0, owner: "Junho Park", admitted: false }),
    A({ name: "arm-shoal-05", state: "retracted", gpus: 4, owner: me, gpu: "A100", vram_mb: 81920, power: 312, mesh_ip: "100.72.0.5" }),
  ];
}
