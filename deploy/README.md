# Deployment and rollback

The worker and Muse adapter have been deployed with camera and `nod` enabled. The operator confirmed successful photo and nod requests through Muse. Other motion intensities and hold-on-stop remain unverified. Companion-originated text replies have not passed live acceptance; audio and automatic motion cues remain disabled. See [acceptance status](../docs/implementation.md) for the remaining gates. The instructions below describe provisioning and rollback without publishing machine-specific configuration.

## Build

Prepare the [pinned SDK worktree](../muse-gadget-sdk-patches/README.md), then build both distributions:

```sh
.venv/bin/python -m build --wheel --no-isolation
.venv/bin/python -m build --wheel --no-isolation artifacts/sdk-worktree/linux
```

Install the companion wheel in **both** environments. Install the patched Muse wheel only in the Muse environment. The worker needs Reachy Mini **1.9.0**, NumPy, Pillow and the device's GStreamer/ALSA bindings and plugins. Preserve the existing environment's system-site-packages policy; do not replace the robot's Python or GStreamer. The adapter needs no Reachy package; the worker needs no Muse package or pairing access.

Keep an unpatched SDK wheel, the old service definition and wheel checksums in private deployment records before switching services. A local patched worktree does not change the installed robot package.

## Configure

Copy [the example](../examples/companion.example.json) into ignored `artifacts/` for development. Relative paths resolve beside that JSON file. Installed service configuration belongs in administrator-managed private configuration; do not commit generated configuration or filled service templates.

For production, set `worker_uid`, `adapter_uid`, `socket_gid` and `run_as` explicitly. Preserve the stock SDK's configured command account in `run_as`. Use an unprivileged worker account with the minimum media access it needs. Verify its ALSA routes and daemon access; do not grant it SDK credentials or sudo. Modules must already be installed locally.

The socket directories must have different owners: worker for `runtime.sock`, root for `muse.sock`. Directories use 0750; sockets use 0660. A shared connection group permits IPC without letting the worker replace the adapter socket. Both ends check expected peer UIDs. Use the corresponding service-managed runtime directories. The adapter's private `state_dir` stores its conversation UUID; the worker needs no access to it. Keep private configuration outside worker-writable directories.

Fill the templates' `@PLACEHOLDER@` values locally into `artifacts/deploy/`. Systemd requires executable paths in installed units; machine-specific values stay out of the public repository. Install the worker unit and a drop-in for the **existing** Muse service, preserving SDK state/environment and legacy socket settings. Check the effective merged service with `systemctl cat` before switching it.

The camera-enabled worker template runs [prepare_camera_socket.py](prepare_camera_socket.py) as a privileged startup step. It waits for the daemon's media endpoint, requires a socket owned by the configured daemon UID, then assigns the companion connection group and mode 0660. Record the original socket group and mode privately before installation. The helper does not grant access to pairing credentials or change home-directory permissions. For a deployment without camera, omit this startup step. The unit follows daemon stops/restarts and is enabled with the daemon; recreation of the camera endpoint without a daemon restart still needs validation.

The launchers do not install units or restart Reachy's daemon. Duplicate socket ownership is refused. The adapter also refuses to take the legacy socket from an active stock service; run through the existing service, not alongside it.

## Validate in order

1. Start the worker. `tools list` must match enabled modules. `status` distinguishes readiness, Muse connectivity and observed hardware availability without opening media or motors.
2. Start the adapter; wait for registration. Ask Muse to call `reachy.status`. Run a typed CLI turn and verify its correlated answer. Stop/start the worker with the same catalog, then test an adapter reconnect. Changed catalogs require adapter restart.
3. Beside Reachy, enable `audio_enabled` in private worker configuration and restart. Check a low audible speaker volume, at most 40%. Test `say --voice`, then `ptt --seconds 5`; recording starts explicitly and ends at the selected duration. Stop from another terminal during capture and playback. Complete ten correct spoken turns.
4. With an observer, test each preset at low intensity before adding it to `checked_presets` and enabling `motion_enabled`. The optional cues module shares the same controller and allowlist. Presets stream small offsets from the current pose; they do not enable torque or enqueue daemon motion tasks. Local stop stops submissions and requests a hold at the observed pose. It is not emergency torque-off, and daemon commands provide no physical-stop acknowledgement. Keep motion disabled until verified on Reachy.
5. Enable `camera_enabled` for an explicit snapshot. Check two visibly changed scenes through Muse. A valid JPEG result does not establish backend image understanding.

Muse turn completion has a 120-second deadline. The CLI applies a 175-second overall deadline including capture, cloud work, download and playback; exceeding it cancels local work without replay. Camera and emote tools allow 40 seconds, including hardware-process startup; observed camera calls took approximately 9–10 seconds on the deployed robot. Muse's subsequent processing adds time before the app displays its answer. MP3 download is bounded to 8 MiB and decoded playback to 60 seconds. Only completed, request-bound replies qualify for speech. Missing native TTS or correlation evidence is a failed feature gate.

Logs omit parameters, captures and conversation content. CLI answers are intentionally visible to the operator; keep redirected output private. GStreamer EOS confirms the local speaker pipeline drained. Actual audibility and acoustic latency require physical measurement.

## Roll back

Stop the Muse service and companion worker. Remove only the project drop-in, restore the recorded stock Muse wheel if needed, reload systemd and restart stock Muse. Retain identity, pairing and the existing command account. Disable the worker unit when returning to stock, and restore the recorded camera socket group and mode after checking its type and owner. Verify registration and `device.health`. Application rollback requires neither re-pairing nor restarting Reachy's daemon/Bluetooth services.
