"""Remote GPU probe. Runs on Colab via `colab exec -f`.

Choice comes from the H3_GPU_CHOICE environment variable
(passed with `colab exec --env H3_GPU_CHOICE=...`).
G4: RTX PRO 6000 Blackwell, >= 92160 MiB.
A100_HIGH_MEM: A100, >= 76800 MiB. A100 40GB is rejected.
Prints one H3_GPU_EVIDENCE: JSON line, plus the legacy G4 OK line
for the G4 case.
"""
import json
import os
import subprocess

CHOICES = {
    "G4": {"tokens": ("RTX PRO 6000", "BLACKWELL"), "min_mib": 92160},
    "A100_HIGH_MEM": {"tokens": ("A100",), "min_mib": 76800},
}

choice = os.environ.get("H3_GPU_CHOICE", "G4").strip().upper()
if choice not in CHOICES:
    raise RuntimeError("GPU_NOT_AUTHORIZED: " + choice)
p = subprocess.run(
    ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
    capture_output=True, text=True, check=True, timeout=30,
)
print("GPU_PROBE", p.stdout.strip())
rows = p.stdout.strip().splitlines()
if len(rows) != 1:
    raise RuntimeError("Expected exactly one GPU")
name, mem = rows[0].rsplit(",", 1)
name = name.strip()
mem_mib = int(mem.strip())
print("H3_GPU_EVIDENCE:" + json.dumps({"name": name, "memory_total_mib": mem_mib, "gpu_count": 1}))
cfg = CHOICES[choice]
if not any(t in name.upper() for t in cfg["tokens"]):
    raise RuntimeError(f"GPU hardware mismatch for {choice}: {name!r}")
if mem_mib < cfg["min_mib"]:
    raise RuntimeError(f"{choice} VRAM insufficient: {mem_mib} MiB (minimum {cfg['min_mib']})")
if choice == "G4":
    print("GPU_H3_G4_OK")
else:
    print("GPU_H3_A100_HIGHMEM_OK")
