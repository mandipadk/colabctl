#!/usr/bin/env python3
"""Phase A — runtime-direct Drive checkpoint (Pillar 3b) live validation.

Validates, against a real Colab Pro account + the user's real Google Drive, the
runtime-direct checkpoint path: inject a short-lived Drive token to the VM, then have
the **runtime** resumable-upload a file straight to Drive and ranged-download it back —
no client memory/bandwidth in the loop. Verifies the round-trip by SHA-256 *on the VM*.

This touches the user's Drive (a different consent surface than the other probes): it
writes one file ``cc_drive_spike.bin`` into the My Drive ``colabctl`` folder. Delete it
afterward if you like. The runtime is always torn down.

ADC user credentials need a *quota project* with the Drive API enabled, or Drive returns
403. Set one (a project you own with Drive API enabled) via ``COLABCTL_QUOTA_PROJECT``:
    gcloud services enable drive.googleapis.com --project=YOUR_PROJECT

Run:  COLABCTL_ENABLE_NATIVE=1 COLABCTL_QUOTA_PROJECT=your-proj \
        uv run --extra native python spikes/phase_a_drive.py
"""

from __future__ import annotations

import asyncio
import os
import traceback

from colabctl.auth import ADCAuthProvider
from colabctl.drive import DriveCheckpointer
from colabctl.models import Accelerator, RuntimeSpec
from colabctl.transport.native import NativeColabTransport

NAME = "cc-drive-spike"
RUNTIME_SRC = "/content/cc_drive_test.bin"
RUNTIME_DST = "/content/cc_drive_restored.bin"
DRIVE_NAME = "cc_drive_spike.bin"
_SIZE_MIB = 5


async def main() -> None:
    auth = ADCAuthProvider()
    transport = NativeColabTransport.create(auth)
    quota_project = os.environ.get("COLABCTL_QUOTA_PROJECT") or None
    checkpointer = DriveCheckpointer(auth, quota_project=quota_project)
    print(f"    quota_project={quota_project!r}", flush=True)
    try:
        print(f"[1] allocate T4 ({NAME}) ...", flush=True)
        await transport.allocate(RuntimeSpec(accelerator=Accelerator.T4, name=NAME))

        print(f"[2] create a {_SIZE_MIB} MiB file on the runtime ...", flush=True)
        await transport.execute(
            NAME,
            f"import os; open({RUNTIME_SRC!r}, 'wb').write(os.urandom({_SIZE_MIB} * 1024 * 1024))",
            timeout=120,
        )

        print("[3] checkpoint runtime → Drive (resumable, runtime-direct) ...", flush=True)
        up = await checkpointer.checkpoint_file(transport, NAME, RUNTIME_SRC, DRIVE_NAME)
        print(f"    UPLOAD: {up}", flush=True)

        print("[4] restore Drive → runtime (ranged) ...", flush=True)
        down = await checkpointer.restore_file(transport, NAME, DRIVE_NAME, RUNTIME_DST)
        print(f"    DOWNLOAD: {down}", flush=True)

        print("[5] verify SHA-256 on the VM ...", flush=True)
        verify = await transport.execute(
            NAME,
            "import hashlib\n"
            f"a = hashlib.sha256(open({RUNTIME_SRC!r}, 'rb').read()).hexdigest()\n"
            f"b = hashlib.sha256(open({RUNTIME_DST!r}, 'rb').read()).hexdigest()\n"
            "print('VERDICT', 'PASS' if a == b else 'FAIL', a[:12], b[:12])\n",
            timeout=120,
        )
        print(f"    {verify.text.strip()}", flush=True)
    except Exception:
        print("DRIVE SPIKE ERROR:\n" + traceback.format_exc(), flush=True)
    finally:
        print("[6] teardown ...", flush=True)
        try:
            await transport.stop(NAME)
            print("    STOP OK", flush=True)
        except Exception as exc:
            print(f"    STOP err: {exc!r}"[:200], flush=True)
        await transport.aclose()


if __name__ == "__main__":
    asyncio.run(main())
