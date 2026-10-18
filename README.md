# msdos-reachy

A standalone Reachy companion powered by Muse. The worker indexes explicitly enabled tools and owns local media/motion. A separate adapter reuses the Muse Gadget SDK's authenticated connection and reconnect lifecycle.

Read [SPEC.md](SPEC.md) for requirements and [the architecture note](docs/companion-runtime.html) for background. Meta's SDK is a third-party dependency. Our only SDK change is the [versioned service-hook patch](muse-gadget-sdk-patches/README.md); custom tools do not require editing `executor.py`.

The implementation includes the registry, lifecycle, authenticated Unix IPC, custom-tool dispatch, correlated text/voice turns, cancellation, camera size limits and optional bounded cues. **The worker and adapter are deployed on Reachy; the operator confirmed successful camera and nod requests through Muse.** The deployed configuration enables camera and the `nod` preset. Audio and automatic motion cues remain disabled. All hardware features remain disabled in the example configuration. Companion-originated chat replies, audio and further motion acceptance remain pending; see [current status](docs/implementation.md).

## Local development

Run these commands from this repository with Python 3.12 or later:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]' build
mkdir -p artifacts
cp examples/companion.example.json artifacts/companion.json
.venv/bin/reachy-companion-worker --config artifacts/companion.json
```

In another terminal:

```sh
.venv/bin/reachy-companion --config artifacts/companion.json tools list
.venv/bin/reachy-companion --config artifacts/companion.json status
.venv/bin/reachy-companion --config artifacts/companion.json invoke reachy.status
.venv/bin/reachy-companion --config artifacts/companion.json stop
```

Without Reachy, status reports the daemon as unavailable; the worker still starts and lists tools. A module implements `register(registry)` and optional async `start(context)` / `stop()`, then is listed in private configuration. See [core tools](src/reachy_companion/tools/core.py) and [state cues](src/reachy_companion/behaviors/cues.py). Startup freezes the catalog; restart the adapter after changing it.

Prepare the [pinned patched SDK](muse-gadget-sdk-patches/README.md) before adapter tests. Run each suite with its own configuration:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q -c artifacts/sdk-worktree/linux/pyproject.toml artifacts/sdk-worktree/linux/tests
python3 scripts/audit_public.py
```

Tests use local sockets and an in-memory fake Muse VM; they never move Reachy or access real credentials. They check the SDK patch's Python 3.9 syntax; deployed interpreter/hardware checks are separate gates. Read [deployment and rollback](deploy/README.md) before running the adapter on a paired device.

`say "Hello"` is designed to wait for a correlated answer; its live reply path is still under investigation after a timeout. Once that gate passes and audio is enabled, `--voice` requests native Muse speech and `ptt --seconds 5` explicitly records one bounded utterance. `new-chat` stops local activity and persists a fresh conversation UUID. `stop` remains available with Muse offline and does not claim to cancel a remote Muse task.

## Public and private files

Public documentation and examples use relative paths. Personal configuration, setup notes, session records, generated units, diagnostics and captures belong in ignored `artifacts/`. The repository needs no Muse SDK token, device identity or personal chat UUID. The public-file audit prints filenames and finding categories without printing suspected secrets.
