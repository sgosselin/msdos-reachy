# Implementation and acceptance status

The standalone package implements the specification's components. The SDK patch is named for its exact upstream revision. Patched SDK and companion wheels are installed on Reachy; the original local upstream checkout remains unchanged. Both companion services are active and enabled for startup. Camera is enabled; audio, motion and optional motion cues are disabled.

The operator confirmed that a photo request through the Muse app worked. Two logged `reachy.camera.capture` calls completed successfully in approximately 9–10 seconds each. A direct worker capture also produced a valid JPEG. This establishes the first app-to-camera round trip; comparison of two deliberately changed scenes remains a separate acceptance test.

| Area | Implemented and checked offline | Remaining device evidence |
| --- | --- | --- |
| Registry and worker | Explicit module loading, definition validation, catalog freeze/hash, lifecycle unwind, status, bounded queues, stop and deadline behavior | Daemon status and camera access work under the deployed worker account; audio/motion access untested |
| SDK integration | Default hooks, injected catalog/factory across reconnect, unchanged built-in delegation; 218 upstream tests pass locally | Custom camera invocation confirmed on the paired robot; controlled reconnect and reboot acceptance remain |
| Local protocol | Peer UIDs, socket ownership/singletons, deadlines, disconnect cancellation, 64 KiB lines, chunked media, process quotas and expiry | Systemd-managed sockets and deployed worker/adapter requests work; service interruption tests remain |
| Conversation | Subscription before submission, events before acknowledgement, nested acknowledgement, strict reply binding, unique generations, replay suppression and local cancellation | Initial companion request failed during subscription; a later typed request timed out in thinking. Diagnose live chat creation/event correlation before audio |
| Audio | PCM16 WAV conversion, bounded transfers, native TTS integration, child-process cancellation, half-duplex control, MP3 decoding and EOS drain implementation | GStreamer plugins/routes, audible playback, physical stop and ten consecutive spoken turns; acoustic latency measurement |
| Motion | Checked-preset gates, one controller, small streamed offsets from current pose, queued-work cancellation | Observe each preset and hold-on-stop before enabling it; no physical-stop acknowledgement is claimed |
| Camera | Explicit capture, timestamp, adaptive JPEG sizing, full serialized result budget | App photo request confirmed by the operator; interpretation of two deliberately changed scenes remains |
| Packaging/privacy | Installable wheel, CLI, deployment templates, rollback instructions, relative public paths, public-file audit | Wheels/private configuration installed; stock SDK wheel and original service backed up; rollback drill pending |

The tests exercise the full worker → local IPC → adapter → SDK Noise → fake VM text path and custom tool dispatch. Other tests cover unrelated/parentless replies, oversized media, malformed messages, stale catalogs, stop before a turn starts, worker channel loss and a blocked hardware child. They do not stand in for device acceptance.

Local verification used Python 3.14 with 52 project tests and 218 upstream SDK tests passing. On Reachy, 35 worker tests passed under Python 3.12 and 16 conversation/SDK integration tests passed under the adapter's Python 3.13. These tests use a fake VM; they do not resolve the live companion chat timeout. The generic SDK seam also passes a Python 3.9 syntax check. CI is configured for Python 3.12, 3.13 and 3.14; a configured workflow is not evidence that those jobs have already run.

The next checkpoint is a correlated typed reply initiated by the companion, followed by controlled reconnect without replay. Keep the working app camera path available while diagnosing that separate path. Then validate native speech with an observer, followed by microphone turns and bounded motion. Follow [deployment and rollback](../deploy/README.md). Milestone 1 is not yet fully accepted.
