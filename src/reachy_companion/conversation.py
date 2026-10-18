"""Muse-only coordinator: no Reachy imports, one turn, strict reply correlation."""

import asyncio
import contextlib
import time
import uuid
from collections import OrderedDict

from .protocol import CLOUD_LIMIT, MP3_LIMIT, Fault, encoded


class Coordinator:
    def __init__(self, chat_id, *, turn_timeout=120, subscribe_timeout=15):
        from musegadget.muse_turn import ReplayScope
        self.chat_id = chat_id
        self.turn_timeout, self.subscribe_timeout = turn_timeout, subscribe_timeout
        self.session = None
        self.generation = str(uuid.uuid4())
        self.active = None
        self.history = OrderedDict()
        self.replay = ReplayScope(limit=2048)
        self.task = None
        self.speech_task = None
        self.speech_claimed = set()

    def attach(self, session):
        self.generation = str(uuid.uuid4())
        self.session = session

    async def detach(self, session):
        if self.session is session:
            self.session = None
            await self.cancel("outcome_unknown", "disconnect")
            self.history.clear()

    def connection(self):
        return {"connected": bool(self.session and self.session.registered.is_set()),
                "generation": self.generation, "active_turn": self.active["turn_id"] if self.active else None}

    def require_connected(self):
        if not self.connection()["connected"]:
            raise Fault("not_connected", "muse")
        return self.session

    def begin(self, *, text=None, wav=None, voice=False):
        session = self.require_connected()
        if self.active or self.speech_task:
            raise Fault("busy", "turn")
        if text is None and wav is None:
            raise Fault("invalid_params", "text")
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text.encode()) > 16000):
            raise Fault("invalid_params", "text")
        from musegadget.muse_turn import MuseTurn
        turn = {"turn_id": str(uuid.uuid4()), "chat_id": self.chat_id, "generation": self.generation,
                "state": "subscribing", "replies": [], "revision": 0, "voice": voice,
                "tracker": MuseTurn(self.chat_id, owns_chat=False, replay_scope=self.replay),
                "user_ids": set(), "seen_ids": set(), "submitted": False}
        self.active = turn
        self.history[turn["turn_id"]] = turn
        while len(self.history) > 16:
            self.history.popitem(last=False)
        self.task = asyncio.create_task(self._run(session, turn, text, wav))
        return self.snapshot(turn)

    def snapshot(self, turn):
        return {k: v for k, v in turn.items() if k in (
            "turn_id", "chat_id", "generation", "state", "replies", "revision", "voice", "error")}

    def changed(self, turn, state=None):
        if state:
            turn["state"] = state
        turn["revision"] += 1

    def _feed(self, turn, event):
        from musegadget.muse_turn import ReplayScope
        if turn["generation"] != self.generation or self.active is not turn:
            return
        ids = ReplayScope.event_ids(event)
        if ids & self.replay.ids:
            return
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            return
        parent = payload.get("reply_to_message_id") or payload.get("parent_message_id")
        message_id = payload.get("message_id") or event.get("message_id") or payload.get("id")
        tracker = turn["tracker"]
        if event.get("event") in ("message.assistant", "delta.message_done", "delta.text_append"):
            reply = tracker.replies.get(message_id)
            parent_reply = tracker.replies.get(parent)
            if tracker.acknowledged:
                if not (parent in turn["user_ids"] or reply and reply.bound or parent_reply and parent_reply.bound):
                    return
            elif not parent and ("message", message_id) not in turn["seen_ids"]:
                return
        # SDK's unowned mode accepts unbound status events: filter these here.
        if event.get("event") in ("agent.status", "task.status"):
            parent = payload.get("reply_to_message_id") or payload.get("parent_message_id")
            reply = turn["tracker"].replies.get(payload.get("message_id"))
            if parent not in turn["user_ids"] and not (reply and reply.bound):
                return
        turn["seen_ids"].update(ids)
        if len(turn["seen_ids"]) > 2048:
            raise Fault("size_limit", "event_ids")
        turn["tracker"].feed(event)
        self._replies(turn)

    def _replies(self, turn):
        replies = [{"message_id": r.message_id, "text": r.text, "done": r.done}
                   for r in turn["tracker"].replies.values() if r.bound]
        if len(encoded(replies)) > CLOUD_LIMIT - 4096:
            raise Fault("size_limit", "reply_text")
        if replies != turn["replies"]:
            turn["replies"] = replies
            self.changed(turn)

    async def _run(self, session, turn, text, wav):
        async def subscribe():
            async for event in session.subscribe_chat(self.chat_id):
                self._feed(turn, event)
            raise Fault("not_connected", "subscription")

        subscription = None
        try:
            async with asyncio.timeout(self.turn_timeout):
                session.chat_subscribed.clear()
                subscription = asyncio.create_task(subscribe())
                ready = asyncio.create_task(session.chat_subscribed.wait())
                try:
                    done, _ = await asyncio.wait((ready, subscription), timeout=self.subscribe_timeout,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if subscription in done:
                        await subscription
                    if not ready.done():
                        raise Fault("timeout", "subscription_ready")
                finally:
                    ready.cancel()
                    await asyncio.gather(ready, return_exceptions=True)
                self.changed(turn, "submitting")
                turn["submitted"] = True
                if wav is None:
                    ack = await session.send_chat(text, self.chat_id, output_modality="voice" if turn["voice"] else "text")
                else:
                    ack = await session.send_voice(wav, self.chat_id, output_modality="voice")
                if not ack.get("ok"):
                    raise Fault("outcome_unknown", "submission")
                response = ack.get("response")
                result = response.get("result", response) if isinstance(response, dict) else None
                if not isinstance(result, dict):
                    raise Fault("invalid_params", "acknowledgement")
                if result.get("session_id", self.chat_id) != self.chat_id:
                    raise Fault("invalid_params", "acknowledgement_chat")
                # Same extraction as the pinned SDK, without relying on a private helper.
                for key in ("message_id", "reply_to_message_id"):
                    value = result.get(key)
                    if isinstance(value, str) and 0 < len(value) <= 512:
                        turn["user_ids"].add(value)
                if not turn["user_ids"]:
                    raise Fault("invalid_params", "acknowledgement_ids")
                turn["tracker"].acknowledge(response)
                self._replies(turn)  # includes events received before the acknowledgement
                self.changed(turn, "thinking")
                while True:
                    if subscription.done():
                        await subscription
                    if (turn["replies"] and all(r["done"] for r in turn["replies"])
                            and turn["tracker"].complete(time.monotonic())):
                        self.changed(turn, "complete")
                        break
                    await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            if turn["state"] not in ("cancelled", "error"):
                turn["error"] = Fault("cancelled", "turn").as_dict()
                self.changed(turn, "cancelled")
            raise
        except Exception as exc:
            fault = exc if isinstance(exc, Fault) else Fault(
                "timeout" if isinstance(exc, TimeoutError) else "outcome_unknown", turn["state"])
            turn["error"] = fault.as_dict()
            self.changed(turn, "error")
        finally:
            if subscription:
                subscription.cancel()
                await asyncio.gather(subscription, return_exceptions=True)
            self.replay.retire(turn["seen_ids"] | {("message", x) for x in turn["user_ids"]})
            turn["tracker"].retire()
            if self.active is turn:
                self.active = None

    async def watch(self, turn_id, generation, revision):
        turn = self.history.get(turn_id)
        if turn is None or generation != self.generation or generation != turn["generation"]:
            raise Fault("not_connected", "stale_turn")
        deadline = time.monotonic() + 10
        while turn["revision"] == revision and turn["state"] not in ("complete", "cancelled", "error"):
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(0.05)
        return self.snapshot(turn)

    async def speech(self, turn_id, generation, message_id):
        session = self.require_connected()
        turn = self.history.get(turn_id)
        if (not turn or generation != self.generation or turn["generation"] != generation
                or turn["state"] != "complete" or not turn["voice"]
                or not any(r["message_id"] == message_id and r["done"] for r in turn["replies"])):
            raise Fault("invalid_params", "speech_reply")
        key = (generation, turn_id, message_id)
        if self.active or self.speech_task or key in self.speech_claimed:
            raise Fault("busy", "speech")
        self.speech_claimed.add(key)  # Claim before downloading; never replay after uncertainty.
        if len(self.speech_claimed) > 256:
            self.speech_claimed = {x for x in self.speech_claimed if x[1] in self.history}
        self.speech_task = asyncio.current_task()
        output = bytearray()
        try:
            async with asyncio.timeout(60):
                async for data in session.stream_tts(message_id):
                    if len(output) + len(data) > MP3_LIMIT:
                        raise Fault("size_limit", "speech")
                    output.extend(data)
            if not output:
                raise Fault("not_connected", "native_tts")
            return bytes(output)
        finally:
            self.speech_task = None

    async def cancel(self, code="cancelled", stage="local_stop"):
        stopped = False
        if self.active:
            self.active["error"] = Fault(code, stage).as_dict()
            self.changed(self.active, "cancelled" if code == "cancelled" else "error")
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            # Cancellation before the task's first scheduling never enters its finally block.
            self.active = None
            stopped = True
        if self.speech_task and self.speech_task is not asyncio.current_task():
            self.speech_task.cancel()
            await asyncio.gather(self.speech_task, return_exceptions=True)
            stopped = True
        # Completed speech eligibility is also retired by local stop.
        for turn in self.history.values():
            if turn["state"] == "complete":
                self.changed(turn, "cancelled")
        return {"local_work_stopped": stopped, "remote_task_cancelled": False}
