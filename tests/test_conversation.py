import asyncio

import pytest

from reachy_companion.conversation import Coordinator
from reachy_companion.protocol import MP3_LIMIT, Fault


def event(message="r1", parent="u1", session="chat", content="Hello"):
    payload = {"session_id": session, "message_id": message, "content": content}
    if parent:
        payload["reply_to_message_id"] = parent
    return {"type": "event", "event": "delta.message_done", "payload": payload}


class FakeSession:
    def __init__(self, events=None, ack=None, ready=True):
        self.registered, self.chat_subscribed = asyncio.Event(), asyncio.Event()
        self.registered.set()
        self.events = events if events is not None else [event()]
        self.ack = ack if ack is not None else {"ok": True, "response": {"result": {"message_id": "u1", "session_id": "chat"}}}
        self.ready = ready
        self.queue = asyncio.Queue()
        self.sent, self.tts_calls = [], []
        self.audio = [b"fake-mp3"]

    async def subscribe_chat(self, session_id):
        assert session_id == "chat"
        if self.ready:
            self.chat_subscribed.set()
        try:
            while True:
                yield await self.queue.get()
        finally:
            self.chat_subscribed.clear()

    async def send_chat(self, text, session_id, output_modality):
        assert self.chat_subscribed.is_set()
        self.sent.append((text, output_modality))
        for value in self.events:
            await self.queue.put(value)
        await asyncio.sleep(.01)  # Deliver events BEFORE the acknowledgement.
        return self.ack

    async def send_voice(self, wav, session_id, output_modality):
        return await self.send_chat("voice", session_id, output_modality)

    async def stream_tts(self, message_id):
        self.tts_calls.append(message_id)
        for chunk in self.audio:
            yield chunk


@pytest.fixture
def coordinator(monkeypatch):
    import musegadget.muse_turn as turns
    monkeypatch.setattr(turns, "REPLY_QUIET_S", .02)
    return Coordinator("chat", turn_timeout=.25, subscribe_timeout=.03)


async def finished(coordinator):
    await asyncio.wait_for(coordinator.task, 1)
    return coordinator.snapshot(next(reversed(coordinator.history.values())))


async def test_early_events_nested_ack_and_play_once(coordinator):
    session = FakeSession()
    coordinator.attach(session)
    accepted = coordinator.begin(text="Hello", voice=True)
    assert accepted["state"] == "subscribing"
    result = await finished(coordinator)
    assert result["state"] == "complete"
    assert result["replies"] == [{"message_id": "r1", "text": "Hello", "done": True}]
    assert session.sent == [("Hello", "voice")]
    args = (result["turn_id"], result["generation"], "r1")
    assert await coordinator.speech(*args) == b"fake-mp3"
    with pytest.raises(Fault, match="busy"):
        await coordinator.speech(*args)
    assert session.tts_calls == ["r1"]


async def test_unrelated_and_parentless_replies_never_spoken(coordinator):
    session = FakeSession([event("phone", session="another-chat"), event("unbound", parent=None),
                           event("other", parent="different-request"), event()])
    coordinator.attach(session)
    coordinator.begin(text="Hi", voice=True)
    result = await finished(coordinator)
    assert [r["message_id"] for r in result["replies"]] == ["r1"]
    with pytest.raises(Fault):
        await coordinator.speech(result["turn_id"], result["generation"], "unbound")


async def test_ack_is_not_an_answer(coordinator):
    session = FakeSession([])
    coordinator.attach(session)
    coordinator.begin(text="Hi")
    result = await finished(coordinator)
    assert result["state"] == "error" and result["error"]["code"] == "timeout"


async def test_subscription_must_be_ready_before_submit(coordinator):
    session = FakeSession(ready=False)
    coordinator.attach(session)
    coordinator.begin(text="Hi")
    result = await finished(coordinator)
    assert result["error"]["stage"] == "subscription_ready"
    assert session.sent == []


@pytest.mark.parametrize("ack", [{"ok": False}, {"ok": True, "response": {}},
    {"ok": True, "response": {"result": {"message_id": "u1", "session_id": "wrong"}}}])
async def test_bad_ack_is_explicit_error(coordinator, ack):
    coordinator.attach(FakeSession(ack=ack))
    coordinator.begin(text="Hi")
    assert (await finished(coordinator))["state"] == "error"


async def test_stop_and_reconnect_retire_turns_and_replay(coordinator):
    session = FakeSession()
    coordinator.attach(session)
    coordinator.begin(text="First", voice=True)
    first = await finished(coordinator)
    await coordinator.detach(session)
    coordinator.attach(session)
    assert coordinator.generation != first["generation"]
    with pytest.raises(Fault):
        await coordinator.watch(first["turn_id"], first["generation"], -1)
    session.ack = {"ok": True, "response": {"message_id": "u2"}}
    session.events = [event(), event("r2", "u2", content="Fresh")]
    coordinator.begin(text="Second")
    second = await finished(coordinator)
    assert [r["text"] for r in second["replies"]] == ["Fresh"]


async def test_cancel_invalidates_completed_speech_and_active_turn(coordinator):
    session = FakeSession()
    coordinator.attach(session)
    coordinator.begin(text="First", voice=True)
    first = await finished(coordinator)
    result = await coordinator.cancel()
    assert not result["remote_task_cancelled"]
    with pytest.raises(Fault):
        await coordinator.speech(first["turn_id"], first["generation"], "r1")
    session.events = []
    coordinator.begin(text="Second")
    with pytest.raises(Fault, match="busy"):
        coordinator.begin(text="Third")
    await coordinator.cancel()
    assert coordinator.active is None


async def test_tts_total_size_limit(coordinator):
    session = FakeSession()
    session.audio = [b"x" * MP3_LIMIT, b"x"]
    coordinator.attach(session)
    coordinator.begin(text="Hi", voice=True)
    result = await finished(coordinator)
    with pytest.raises(Fault, match="size_limit"):
        await coordinator.speech(result["turn_id"], result["generation"], "r1")
