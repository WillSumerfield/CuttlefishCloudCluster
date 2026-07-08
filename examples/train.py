"""Minimal CCC validation Spawn: prove GPU reaches the Pod + (optionally) log to W&B.

Run via:  cuttle run examples/train.py --gpu 1 -f
Exits non-zero if CUDA isn't visible inside the Pod (that's the thing we're validating).
"""

import os
import time

import torch

print(f"torch {torch.__version__}")
assert torch.cuda.is_available(), "CUDA not visible in Pod — GPU passthrough failed"
dev = torch.device("cuda")
print(f"device: {torch.cuda.get_device_name(dev)}  (count={torch.cuda.device_count()})")

# A real GPU op, timed — confirms compute actually runs on-device.
a, b = torch.randn(4096, 4096, device=dev), torch.randn(4096, 4096, device=dev)
torch.cuda.synchronize()
t0 = time.time()
for _ in range(20):
    c = a @ b
torch.cuda.synchronize()
print(f"20x 4096^2 matmul on GPU: {time.time() - t0:.3f}s  (checksum {c.sum().item():.1f})")

# Optional W&B — base image may not ship wandb; log only if importable + enabled.
if os.environ.get("WANDB_MODE") != "disabled":
    try:
        import wandb

        wandb.init(entity="Will@ARIA", name=os.environ.get("WANDB_NAME"))
        for step in range(10):
            wandb.log({"loss": 1.0 / (step + 1)}, step=step)
        wandb.finish()
        print("W&B: logged 10 steps")
    except ImportError:
        print("W&B: wandb not installed in Pod — skipped")
else:
    print("W&B: disabled")

print("spawn OK")
