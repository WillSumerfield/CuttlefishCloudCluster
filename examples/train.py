"""Minimal CCC validation Spawn: prove GPU reaches the Pod, results come back, and (optionally) W&B.

Run via:  cuttle run examples/train.py --time 10 --vram 8 -f
          cuttle outputs <id>          # result.txt, written below
Exits non-zero if CUDA isn't visible inside the Pod (that's the thing we're validating).

Writing to $CCC_OUT is the whole return path: on a remote Arm that directory is what rsync stages back
to the Head, so a validation Spawn that only printed would leave the results path untested.
"""

import os
import time
from pathlib import Path

import torch

# The Mantle: every Spawn on every node executes as this one pinned uid (60000), never as the
# submitter. Printed because it must be identical on the Head and on any donor's Arm.
print(f"uid {os.getuid()} ({os.environ.get('SLURMD_NODENAME', '?')})")
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
elapsed, checksum = time.time() - t0, c.sum().item()
print(f"20x 4096^2 matmul on GPU: {elapsed:.3f}s  (checksum {checksum:.1f})")

# The result the Spawn hands back. $CCC_OUT is bound into the Pod by the job script and is the ONLY
# path out — anything written elsewhere dies with the scratch dir.
out = Path(os.environ["CCC_OUT"])
(out / "result.txt").write_text(
    f"node {os.environ.get('SLURMD_NODENAME', '?')}\n"
    f"uid {os.getuid()}\n"
    f"device {torch.cuda.get_device_name(dev)}\n"
    f"20x 4096^2 matmul {elapsed:.3f}s\n"
    f"checksum {checksum:.1f}\n"
)
print(f"wrote {out}/result.txt")

# Optional W&B — base image may not ship wandb; log only if importable + enabled.
if os.environ.get("WANDB_MODE") != "disabled":
    try:
        import wandb

        # entity + project come from WANDB_ENTITY / WANDB_PROJECT, injected per-Spawn by `cuttle run`.
        wandb.init(name=os.environ.get("WANDB_NAME"))
        for step in range(10):
            wandb.log({"loss": 1.0 / (step + 1)}, step=step)
        wandb.finish()
        print("W&B: logged 10 steps")
    except ImportError:
        print("W&B: wandb not installed in Pod — skipped")
else:
    print("W&B: disabled")

print("spawn OK")
