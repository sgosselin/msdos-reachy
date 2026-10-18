# Reachy companion runtime specification

Version 0.1 · Implementation contract · 17 October 2026

This specification defines the software we will build in `msdos-reachy`. It refines the earlier [architecture note](docs/companion-runtime.html) and defines the first milestone: reliable embodied turns. “Must” identifies an acceptance requirement; named interfaces below are proposed unless explicitly identified as existing SDK APIs. Writing this specification does not deploy software or change the robot.

## 1. Purpose and scope

Build a local companion runtime that loads our companion code, discovers its declared tools, makes those tools available to Muse, and owns Reachy's conversational behavior. Muse supplies reasoning and conversation; our runtime supplies reliable hardware execution, media handling and local lifecycle control.

The first complete experience is push-to-talk speech through Reachy's microphone and speaker, with gentle listening/thinking/speaking cues, explicit stop, recovery without replay, and an on-demand camera-understanding check. Before that, the first implementation increment must register and execute one read-only custom tool and receive a correlated text reply.

| In scope for version 1 | Deferred |
| --- | --- |
| Explicit module configuration; local tool registry; typed invocation and results | Loading arbitrary code downloaded by Muse; plugin marketplace; live code reload |
| One Reachy, one Muse connection, one active companion conversation | Cross-device orchestration, parallel spoken conversations, VM provisioning |
| Push-to-talk, native Muse speech, bounded local playback | Always-on listening, spoken interruption, echo cancellation, special voice effects |
| Camera snapshots and bounded motion presets | Continuous video, visual tracking, autonomous exploration |
| Startup/shutdown, diagnostics, cancellation and reconnect handling | Long-term task memory and the later collaboration milestones |

Prototype checks established pairing, registration, a health answer and camera capture. The companion implementation now covers the registry, IPC, adapter, conversation and gated hardware paths; deployment and physical acceptance remain separate checks. See [implementation status](docs/implementation.md) for evidence and outstanding gates. Machine-specific setup and session records belong in ignored `artifacts/`, outside this public specification. Public specifications and documentation use relative repository paths only; never include personal filesystem paths, credentials, device identifiers or conversation UUIDs.

## 2. Architecture and ownership

```text
                         Muse
           tool invocations / chat events / speech
                           ⇅
        Muse adapter — our launcher + pinned Muse SDK
        identity, credentials, reconnect, registration
                           ⇅
             bounded local Unix-socket protocol
                           ⇅
        Companion worker — our unprivileged runtime
      registry, behavior, turns, audio, camera, motion
                           ⇅
                  Existing Reachy daemon
```

One process owns the gadget's authenticated Muse connection. The worker uses that process for all cloud communication. Pairing and device credentials remain in the adapter; companion modules execute in the worker. Existing Reachy daemon and media interfaces remain the hardware foundation.

The adapter runs in the Muse environment, currently system Python 3.13.5. The worker uses the Reachy app environment, currently Python 3.12.12 with Reachy SDK 1.9.0. Shared protocol/configuration code must work in both environments. The worker account has no sudo permissions; verify the specific media/socket access it needs before deployment rather than granting broad privileges.

### Components we create

| Component | Responsibility and requirements |
| --- | --- |
| **Configuration and module loader** | Load an explicit, ordered allowlist of installed Python modules from local configuration. Resolve packages locally; report missing/incompatible modules before becoming ready. Keep configuration versioned and credentials out of it. |
| **Tool registry** | Index each declared tool by its unique public name. Validate definitions, parameter types and limits; reject duplicates and built-in name collisions. Produce a serializable catalog and catalog hash. Provide a local `tools list` command. |
| **Companion worker** | Own module start/stop, shared context, events, tasks and hardware resources. Route tool calls to handlers; enforce deadlines and cancellation. Serialize motion and playback while allowing read-only status calls. |
| **Muse adapter** | Reuse the SDK's authentication, refresh, reconnect and transport. Merge the built-in catalog with worker tool metadata, route custom calls over local IPC, and expose chat/voice/event/TTS operations. Never import worker plugins into the privileged process. |
| **Local protocol** | Carry versioned requests, results, events and bounded media transfers. Correlate requests, propagate deadlines, handle disconnects and reject oversized or malformed data. Preserve the stock text socket separately. |
| **Conversation controller** | Persist the companion chat UUID, manage one active turn, subscribe before submitting, correlate replies, prevent stale playback and drive the idle/listening/thinking/speaking/offline states. |
| **Reachy adapters** | Capture microphone/camera data, decode/resample/play audio, and perform bounded head/antenna motions through one controller. Check daemon/app ownership before acquiring media. |
| **Operator interface and deployment** | Supply local CLI commands for status, tool listing, typed requests, push-to-talk and stop. Provide service units/drop-ins, diagnostics and rollback without replacing pairing state. |

## 3. Modules and tool requirements

### R1 — Declared modules, indexed tools

Each configured module exposes a proposed `register(registry)` function and optional asynchronous `start(context)` / `stop()` lifecycle functions. Registration declares tools without moving motors, starting recordings or contacting Muse. Start configured modules in order; stop them in reverse order. A failed startup must unwind already-started modules and report the failure.

Each tool definition includes:

- A stable name, description and tool version.
- Required/optional parameter definitions, supported types, and local validation constraints.
- A default deadline and a bounded result size.
- A concurrency class (`read`, `motion` or `media`) and cancellation behavior.
- A handler receiving validated arguments, invocation context and a cancellation token.

Use the SDK's existing `description`, `required`, `optional` and `timeout_ms` command format when advertising. Keep our version, scheduling and validation metadata local unless the backend's support for additional fields is established. Do not equate the SDK format with unrestricted JSON Schema support. For v1, primitive argument types are sufficient.

The registry is a name-to-definition/handler map, not a semantic search index. A module must explicitly declare every public tool. Configuration changes require a controlled restart; there is no hot registration or arbitrary remote import in v1.

### R2 — Initial tools and execution

| Tool | Contract | Delivery stage |
| --- | --- | --- |
| `reachy.status` | No required arguments. Return runtime version, readiness, connection/turn state, and observed Reachy daemon/media availability. Distinguish observed state from unavailable or untested capabilities. No media capture or movement. | First implementation increment |
| `reachy.stop` | No required arguments. Cancel queued/active local audio and gestures and invalidate the active turn. Idempotent; report whether local work actually stopped. Does not claim to cancel Muse's remote task. | Before media/motion deployment |
| `reachy.emote` | Required named preset; optional bounded intensity. Accept only local presets whose ranges/durations have been checked on the robot. Reject arbitrary joint commands. | Basic embodiment |
| `reachy.camera.capture` | Optional bounded size/quality settings. Return capture time and a size-limited JPEG result; fail clearly if capture is unavailable. Backend image interpretation is a separate acceptance gate. | Camera feasibility |

Retain `device.health`, `system.run`, `file.read` and `file.write` initially through the stock `Executor`; do not redefine them. Shell/file calls continue to use `muse-reachy` permissions. Custom tool dispatch goes to the worker. Convert handler failures into bounded SDK-compatible error results; never expose credentials or raw tracebacks in tool results.

The SDK can dispatch four invocations concurrently. Our worker must apply its own hardware arbitration. Stop must bypass ordinary worker queues. Cancelling a waiting Python task alone is not proof that hardware stopped: adapters must acknowledge cancellation and stop submitting work. A deadline must prevent queued work from executing later.

### R3 — Companion behavior

Companion modules may run local routines as well as tool handlers. Their context provides the registry, hardware adapters, conversation operations, runtime events and cancellation facilities. Background tasks must be registered with the runtime so shutdown can cancel and await them. They must not create their own Muse sessions or independent motion/playback controllers.

## 4. Muse SDK integration

### Existing interfaces we reuse

Baseline: `facebookincubator/muse-gadget-sdk` revision `f60347aeb851a1fe9438cfc388721776aa24f275` (installed package reports 0.1.0).

| SDK integration point | Existing behavior | Our integration |
| --- | --- | --- |
| [executor.py](../muse-gadget-sdk/linux/src/musegadget/executor.py), `COMMAND_SPECS`, `Executor.run()` | Declares/executes built-ins, including `device.health` | Compose a dispatcher: built-ins delegate to `Executor`; custom names forward to the worker. No edits to `executor.py`. |
| [link_client.py](../muse-gadget-sdk/linux/src/musegadget/link_client.py), `DeviceDescription(commands=...)` | Sends supplied tool definitions as `commands_v2` in `link.register` | Supply a validated snapshot containing built-ins plus our registered tools. |
| `LinkSession(run_command=...)` | Dispatches a synchronous callback in a thread pool, then returns `link.result` | Use the composite dispatcher's `run` method. Its IPC wait is bounded; never block the adapter's asyncio loop. |
| [service.py](../muse-gadget-sdk/linux/src/musegadget/service.py), `Service.run()` | Loads pairing, refreshes credentials, discovers a VM and reconnects | Reuse this lifecycle. Add only the injection points below. |
| `send_chat()`, `send_voice()`, `subscribe_chat()`, `stream_tts()` | Text/voice submission, chat event subscription, MP3 retrieval on an existing session | Expose these through our adapter to the worker, using the same authenticated session. |
| [muse_turn.py](../muse-gadget-sdk/linux/src/musegadget/muse_turn.py), `MuseTurn`, `ReplayScope` | Correlates acknowledgements/replies and filters replayed IDs | Use in the adapter's per-chat coordinator; forward normalized turn events to the worker. |
| `Service.serve_local()` and the existing CLI | One JSON-line text request and acknowledgement | Preserve the SDK-configured legacy socket and `musegadget send-user-msg` behavior. New companion operations use our separate protocol. |
| `config`, `identity`, `pair`, and `muse_api` | Identity, pairing and token management | Preserve existing storage and setup commands. Do not reimplement cryptography or credential refresh. |

### Preferred integration: two optional service hooks

The library already accepts custom tools. The stock service hardcodes `COMMAND_SPECS` and constructs `LinkSession` directly. Make those two choices injectable while preserving all default behavior.

The reviewable [proposed patch](muse-gadget-sdk-patches/f60347aeb851a1fe9438cfc388721776aa24f275.patch) adds **three lines and replaces two existing lines** in `linux/src/musegadget/service.py`:

```python
from typing import Callable

# Append to Service's existing dataclass fields:
command_specs: dict = field(default_factory=lambda: COMMAND_SPECS.copy())
session_factory: Callable[..., LinkSession] = LinkSession

# In Service._session(), replace the two hardcoded choices:
commands=self.command_specs,
session = self.session_factory(...)
```

Keep the patch in `muse-gadget-sdk-patches/<upstream-commit>.patch`, named for the full upstream commit it applies to. Apply and test it in an isolated worktree, preserving the upstream checkout and deployed baseline. A new upstream revision requires its own reviewed patch and compatibility checks. The line count excludes tests, documentation and our own launcher/runtime code. Keep the factory keyword contract identical to the current `LinkSession` constructor; defaults must preserve the stock CLI and service behavior. Append the fields so existing positional constructor arguments do not shift. Treat the supplied catalog as immutable for the service lifetime; our adapter deep-copies and validates it before construction.

Our launcher will construct the service approximately as follows (pseudocode; `CompositeExecutor`, `bridge` and `catalog` are ours to implement):

```python
service = Service(
    identity=identity.load_or_create(),
    executor=CompositeExecutor(builtin_executor, bridge),
    sdk_token=config.sdk_token(),
    command_specs=catalog,
    session_factory=bridge.make_session,
)
# Launcher owns signals, the legacy socket, companion IPC and shutdown.
await service.run()
```

`bridge.make_session(**kwargs)` creates a thin `LinkSession` subclass that delegates transport to `super().run(stop)` and uses `try/finally` to attach/detach our coordinator. Each attempt gets a new connection generation. The coordinator waits for `session.registered` before accepting cloud work; disconnect clears its active reference, cancels subscriptions/transfers and invalidates affected turns. This avoids reading `Service._current` from our code or copying the reconnect/token-refresh loop.

Our dispatcher is supplied through the existing `executor` argument: no third SDK hook is needed. SDK callbacks run in worker threads, so forwarding into an asyncio IPC client must use a thread-safe submission and a deadline. Session registration and background stream tasks must remain responsive while a tool is running.

### Alternative with zero SDK edits

Keep our earlier `Service` subclass approach and override `_session()` in the project adapter to supply the catalog and session class. This avoids an upstream patch but duplicates roughly 25–35 lines of SDK lifecycle glue and relies on private fields. Prefer the two hooks for a smaller maintenance boundary. Neither approach requires changing `executor.py`, the wire protocol, pairing or Noise code.

Thus, “a few lines” is realistic for the **SDK seam**. The companion registry, IPC, scheduling, media and tests remain substantive project code; the stock daemon's text socket cannot provide those capabilities by itself.

## 5. Local integration contract

### R4 — Endpoints, messages and limits

Use two permission-restricted Unix sockets in separate service-managed runtime directories. Paths below are relative to the owning service's runtime directory, supplied by deployment configuration:

- `runtime.sock`: worker-owned endpoint for catalog discovery, tool invocation, runtime status and local stop. The adapter and operator CLI connect here. Local stop remains available if Muse or its adapter is unavailable.
- `muse.sock`: root-owned adapter endpoint, accessible to the worker's group, for text/voice submission, correlated events and speech transfers. Authentication uses filesystem permissions and expected local peer identity, not tokens copied out of the SDK.

Use socket mode 0660 and directory mode 0750 with the appropriate owner/group. The worker must not be able to replace the adapter endpoint. The privileged adapter must not blindly unlink or follow arbitrary worker-controlled paths. Do not expose these endpoints on TCP in v1.

For v1, use UTF-8 newline-delimited JSON, **64 KiB maximum per line**, including media chunks encoded as base64. This simplifies the first implementation; a binary transport can be added later without changing application operations. Every request has protocol version, UUID request ID, operation, payload and deadline. Responses echo the ID and contain either a result or a structured error. Events carry a connection generation, chat ID and turn ID where applicable.

| Endpoint | Proposed operations |
| --- | --- |
| Worker | `catalog.get`, `runtime.status`, `tool.invoke`, `runtime.stop`, `conversation.run` |
| Adapter | `connection.status`, `chat.send`, `voice.send`, `turn.watch`, `turn.cancel_local`, `speech.read`, `chat.new` |
| Either, for declared transfers | `transfer.begin`, `transfer.chunk`, `transfer.end`, `transfer.read`, `transfer.abort` |

These names are our interfaces, not existing Muse endpoints. `turn.cancel_local` only cancels our coordinator/stream work; `runtime.stop` handles hardware cancellation directly.

Transfers must declare purpose, type, total bytes and transfer ID before chunks arrive. Use at most 16 KiB raw data per chunk with contiguous offsets. Reject unknown transfers, mismatched lengths and excess data before buffering. Limit each process to three live transfers and 12 MiB of raw transfer buffers; apply idle and overall deadlines; discard partial transfers on disconnect. No caller-supplied arbitrary filesystem paths are accepted as media references. Large tool results, including JPEGs, use transfer references on local IPC; the adapter resolves them into the bounded cloud result. They must not bypass the 64 KiB JSON-line limit.

Initial limits (project defaults, adjustable only with matching tests): one active turn; one voice upload and one speech download; 20-second recording maximum; 2 MiB WAV maximum; 8 MiB total MP3 per reply; 60-second decoded playback maximum. Subscription readiness waits at most 15 seconds after registration; a turn waits at most 120 seconds for completion. Hardware tool deadlines are tool-specific; `reachy.status` defaults to 3 seconds. On timeout, report the actual failed stage and suppress late effects.

Initial adapter startup waits at most 30 seconds for a valid worker catalog before reporting not-ready and retrying. Once registered, loss of the worker fails custom calls with `worker_unavailable`; built-ins can continue. A restarted worker must match the registered catalog hash before serving calls. A changed catalog requires a coordinated reconnect/restart, not silent metadata replacement.

### R5 — Result and retry semantics

Use explicit errors such as `invalid_params`, `unknown_tool`, `busy`, `worker_unavailable`, `not_connected`, `timeout`, `cancelled` and `size_limit`. Translate them to the SDK's supported result envelope at the cloud boundary.

Track requests and terminal outcomes in bounded caches scoped to a connection generation. Never automatically replay a tool action or chat submission after an ambiguous disconnect: the remote side may already have accepted it. Report `outcome_unknown` when necessary. Discard responses and media from an old generation. A fresh operator turn is a new request, not recovery of an uncertain old one.

## 6. Conversation, media and hardware requirements

### R6 — Chats and turn correlation

Persist a chat UUID in private runtime state and reuse it across restarts. Configure any existing chat UUID locally; never embed a personal conversation identifier in source or public documentation. `reachy-voice` is a local label; the live server rejected it as an API session ID. A deliberate “new conversation” operation generates/persists a new UUID; the SDK documents creating a side chat on first submission. Verify the resulting chat in the live backend. V1 does not implement cloud chat listing or deletion.

For each runtime-originated turn:

1. Require a registered session and start the chat subscription; wait for `chat_subscribed` before sending. Create `MuseTurn` before submission so early events are retained.
2. Submit text or WAV on that session. Check the returned `ok` status; feed its nested `response` to `MuseTurn.acknowledge()`, not the entire transport wrapper.
3. Feed subscribed events to the coordinator. Use `ReplayScope`, request IDs and connection generations to reject unrelated/replayed responses.
4. Default to explicit request/parent-bound replies (`Reply.bound`); a dedicated chat ID alone does not prove that no other writer is using it. Use `owns_chat=True` only with enforced writer exclusivity and the SDK's required matching `session_id` / `is_thread` acknowledgement. If the live response lacks evidence for the selected mode, fail explicitly and investigate; do not silently loosen correlation to play arbitrary messages.
5. Forward normalized status/reply/completion events to the worker. Only matching, eligible replies may enter its playback queue. Speech from unrelated phone-app conversations must not play automatically.

The CLI's successful acknowledgement means submitted, not answered. `MuseTurn.complete()` and observed reply content determine completion, with a separate bounded deadline. Keep a completed-message playback record for active/recent turns. After process restart, begin without an active turn and do not resume historical speech.

### R7 — Voice and embodiment

Capture/playback stay on Reachy. Convert the measured microphone format to mono PCM16 WAV, initially 16 kHz, with at most 20 seconds per turn. Request `output_modality="voice"` through `send_voice()` or `send_chat()`; use `stream_tts(message_id)` for the corresponding completed reply. Decode MP3, resample to the actual speaker format, and distinguish queued samples from fully drained playback.

Start half-duplex: microphone capture is disabled during playback. A local stop cancels capture, queued speech and motion, closes pending media transfers and retires the local turn. Do not retransmit incomplete speech automatically. If native TTS is unavailable, report it as a failed feature gate rather than claiming the voice milestone is complete.

The worker owns these states: `starting`, `idle`, `listening`, `thinking`, `speaking`, `offline`, `stopping` and `error`. Connectivity and turn transitions drive them; a request acknowledgement alone does not imply speaking. One motion controller owns all project gestures. Preset amplitudes/durations must be checked on the physical robot before enabling them.

### R8 — Camera

Capture only on explicit request. Attach capture time and enforce the serialized cloud tool-result limit, including base64 and JSON overhead. Target at most 240 KiB for the whole result, below the SDK's documented 256 KiB limit. Reject or downscale an image that does not fit; the earlier 270,880-byte JPEG cannot be sent unchanged. Validate the existing ESP32 JPEG-result precedent on the live Linux path. Two changed scenes must produce answers grounded in fresh images before claiming camera understanding.

## 7. Deployment and observability

### R9 — Startup, compatibility and rollback

Package our code in this repository and pin both SDK versions/revisions in deployment records. With the hook approach, build the minimally patched Muse package from an isolated branch. Do not edit installed site-packages manually.

After offline tests pass, replace the stock `musegadget.service` entrypoint through a project-managed systemd drop-in that runs our launcher in the Muse environment. Preserve the SDK-configured state directory, its ownership, the configured command account and the legacy text socket. Start the worker through its own unit in the Reachy environment; use readiness/IPC handshakes rather than relying on service ordering alone. Ensure only one adapter owns the identity/socket; refuse duplicate startup. Repository paths are relative; machine-specific executable, runtime and state locations come from private deployment configuration or the SDK's defaults.

Rollback stops our worker/adapter, removes the project entrypoint override, and restores the recorded stock SDK package if patched. Restart the stock Muse service using the same identity and pairing. No re-pairing or Reachy daemon restart should be needed for this application rollback. Keep the existing camera/audio diagnostics usable independently.

### R10 — Evidence and diagnostics

Log component/version, catalog hash, connection generation, request/turn ID, tool name, stage durations and bounded error codes. Provide a readable status command that separates connection health, worker readiness and hardware availability. Do not log tokens, raw audio/images or conversation content by default. Measure recording-end → first-audible-sample latency without claiming a target until measured on Reachy.

## 8. Implementation sequence and acceptance

| Increment | Deliverable | Required evidence |
| --- | --- | --- |
| **A. Registry and worker** | Package skeleton, explicit modules, read-only `reachy.status`, local CLI and lifecycle | Tool list matches enabled modules; duplicate/invalid definitions fail; startup unwinds on failure; status works without Muse and without motion/media capture. |
| **B. SDK seam and IPC** | Two service hooks, launcher, composite dispatch and bounded protocol | Stock defaults and legacy socket still pass; custom catalog is advertised; built-ins delegate unchanged; reconnect uses the injected factory; malformed/oversized IPC is rejected; timeout/disconnect cannot trigger late handler effects. |
| **C. First live integration** | Muse calls `reachy.status`; runtime sends text and receives its own answer | Correlated invocation/result in logs, matching live reply, successful stop/start and one controlled reconnect with no replay. Keep a working rollback. |
| **D. Voice** | Typed request → native speech, then push-to-talk → speech | Actual observer-confirmed audio checks, valid WAV conversion, event-before-ack handling, unrelated/replayed reply rejection, local cancellation and ten correct consecutive turns. |
| **E. Embodiment and vision** | Bounded presets, stop and fresh camera tool | One checked motion with stop; stop preempts queued work; two changed scenes interpreted correctly; oversized/unavailable capture fails clearly. |

Run the upstream suite for the SDK hook change (baseline: 218 passed on Reachy). Add focused tests for default/custom construction, catalog injection and session-factory reuse; test Python 3.9 compatibility of the generic hook as well as the deployed Python. Pairing/Noise code is outside the patch. Test our transport/coordinator against the SDK's fake-VM fixtures before hardware deployment.

The implementation follows A through E, with offline evidence recorded separately from device acceptance. The next device checkpoint is increment C: deploy with media/motion disabled, invoke `reachy.status` from Muse and obtain a correlated text answer. Hardware tests remain observed checkpoints; milestone 1 requires evidence for all five increments, including ten consecutive correct spoken turns, reconnect without replay and fresh-image understanding.

## 9. Proposed repository layout

```text
msdos-reachy/
  SPEC.md                         # This specification
  pyproject.toml                  # Package + adapter/worker/CLI entrypoints
  src/reachy_companion/
    config.py                     # Local module/behavior/chat configuration
    protocol.py                   # Shared bounded message/transfer types
    registry.py                   # Definitions, validation, catalog/index
    runtime.py                    # Worker lifecycle and task ownership
    ipc.py                        # Socket endpoints and request/transfer routing
    muse_adapter.py               # SDK launcher, factory, composite executor
    conversation.py               # Chat coordinator and normalized events
    cli.py                        # Operator commands
    hardware/                     # Reachy camera/audio/motion adapters
    tools/                        # status, stop, emote, camera
    behaviors/                    # Conversational state → local behavior
  tests/                          # Registry, IPC, lifecycle, correlation, media
  deploy/                         # Worker unit, Muse drop-in, rollback notes
  muse-gadget-sdk-patches/         # <upstream-commit>.patch + upgrade instructions
```

The package must keep SDK/hardware imports in their respective components so installing the adapter does not import Reachy's media stack, and the worker does not need access to Muse credentials. All project-specific behavior remains outside Meta's SDK.
