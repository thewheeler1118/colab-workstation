"""Remote probe: confirm google.colab.drive.mount accepts timeout_ms.

Run by h3_controller before the official `colab drivemount` step. The
vendored CLI patch injects drive.mount(path, timeout_ms=600000); if the
remote runtime's drive.mount does not accept that keyword, the mount would
fail fast with TypeError. This probe fails closed first, before any mount
attempt, so the failure is diagnosable instead of burning a mount window.
"""
import inspect
from google.colab import drive

print("MOUNT_SIGNATURE: " + str(inspect.signature(drive.mount)), flush=True)
