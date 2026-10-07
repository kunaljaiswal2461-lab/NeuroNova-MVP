"""SPEC-03 §16.3 — cassette tests C1–C8, plus guard tests B2."""
from __future__ import annotations

import socket

import pytest

from agentic_engine.llm_engine.base_llm import ChatResult, LLMClient, StreamDelta
from evals import guards
from evals.cassette import ACK_ENV, ACK_VALUE, CassetteError, CassetteLLM, CassetteMiss

FIXTURE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://nn:pw@app-postgres.nn-eval.test:5432/nn_app",
    ACK_ENV: ACK_VALUE,
}
CALL = dict(model="m1", system="You write SQL.", user="count orders", max_tokens=100, temperature=0.0)


class FakeInner:
    api_key = "sk-THIS-MUST-NEVER-BE-STORED-000000"

    def __init__(self):
        self.calls = 0

    async def chat_json(self, *, model, system, user, max_tokens=1500, temperature=0.2):
        self.calls += 1
        return ChatResult(content_json={"sql": "SELECT 1", "n": self.calls}, model=model, input_tokens=11, output_tokens=7)

    async def chat_stream(self, *, model, system, user, history=None, max_tokens=1500, temperature=0.4):
        self.calls += 1
        for part in ("Hel", "lo ", "there"):
            yield StreamDelta(text=part)
        yield StreamDelta(finished=True, input_tokens=5, output_tokens=3, model=model)


class BrokenInner(FakeInner):
    async def chat_json(self, **kw):
        raise RuntimeError("provider exploded")


def record(path, inner=None):
    return CassetteLLM(path, "record", inner or FakeInner(), env=FIXTURE_ENV)


async def collect(stream):
    return [d async for d in stream]


async def test_c1_record_then_replay_chat_json(tmp_path):
    path = tmp_path / "c.jsonl"
    recorded = await record(path).chat_json(**CALL)
    replay = CassetteLLM(path, "replay")
    replayed = await replay.chat_json(**CALL)
    assert replayed == recorded
    assert isinstance(replay, LLMClient)


async def test_c2_changed_system_prompt_misses_and_names_nearest(tmp_path):
    path = tmp_path / "c.jsonl"
    await record(path).chat_json(**CALL)
    replay = CassetteLLM(path, "replay")
    with pytest.raises(CassetteMiss) as exc:
        await replay.chat_json(**{**CALL, "system": "You write SQL. Always quote identifiers."})
    msg = str(exc.value)
    assert "re-record" in msg and "system" in msg


def test_c3_replay_with_inner_raises_at_construction(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text("")
    with pytest.raises(CassetteError):
        CassetteLLM(path, "replay", FakeInner())


async def test_c4_replay_makes_no_network_call(tmp_path, monkeypatch):
    path = tmp_path / "c.jsonl"
    await record(path).chat_json(**CALL)

    def no_network(*a, **kw):
        raise AssertionError("network used in replay mode")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    result = await CassetteLLM(path, "replay").chat_json(**CALL)
    assert result.content_json["sql"] == "SELECT 1"


async def test_c4_miss_in_replay_never_falls_through(tmp_path):
    path = tmp_path / "c.jsonl"
    await record(path).chat_json(**CALL)
    with pytest.raises(CassetteMiss):
        await CassetteLLM(path, "replay").chat_json(**{**CALL, "user": "something unrecorded"})


async def test_c5_stream_replay_same_deltas_one_terminal(tmp_path):
    path = tmp_path / "c.jsonl"
    kw = dict(model="m1", system="s", user="u", history=[{"role": "user", "content": "hi"}], max_tokens=50, temperature=0.4)
    live = await collect(record(path).chat_stream(**kw))
    replayed = await collect(CassetteLLM(path, "replay").chat_stream(**kw))
    assert replayed == live
    assert [d.text for d in replayed if not d.finished] == ["Hel", "lo ", "there"]
    assert sum(d.finished for d in replayed) == 1 and replayed[-1].finished


async def test_c5_history_is_part_of_the_key(tmp_path):
    path = tmp_path / "c.jsonl"
    kw = dict(model="m", system="s", user="u", max_tokens=5, temperature=0.1)
    await collect(record(path).chat_stream(history=[{"role": "user", "content": "a"}], **kw))
    with pytest.raises(CassetteMiss):
        await collect(CassetteLLM(path, "replay").chat_stream(history=[{"role": "user", "content": "b"}], **kw))


def test_c6_record_requires_ack(tmp_path):
    env = {k: v for k, v in FIXTURE_ENV.items() if k != ACK_ENV}
    with pytest.raises(CassetteError, match="STAGE4C_EVALUATION_ACK"):
        CassetteLLM(tmp_path / "c.jsonl", "record", FakeInner(), env=env)


def test_c6_record_requires_fixture_hosts(tmp_path):
    env = {**FIXTURE_ENV, "DATABASE_URL": "postgresql+asyncpg://u:p@prod.example.com:5432/app"}
    with pytest.raises(guards.FixtureGuardError):
        CassetteLLM(tmp_path / "c.jsonl", "record", FakeInner(), env=env)


async def test_c7_no_secrets_in_file(tmp_path):
    path = tmp_path / "c.jsonl"
    await record(path).chat_json(**CALL)
    await collect(record(path).chat_stream(model="m", system="s", user="u"))
    text = path.read_text()
    for needle in ("api_key", "Authorization", "sk-"):
        assert needle not in text


async def test_c8_interrupted_record_leaves_previous_file_intact(tmp_path):
    path = tmp_path / "c.jsonl"
    await record(path).chat_json(**CALL)
    before = path.read_bytes()
    with pytest.raises(RuntimeError):
        await record(path, BrokenInner()).chat_json(**{**CALL, "user": "another"})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


async def test_c8_failed_write_leaves_previous_file_intact(tmp_path, monkeypatch):
    import os

    path = tmp_path / "c.jsonl"
    await record(path).chat_json(**CALL)
    before = path.read_bytes()

    def fail(*a, **kw):
        raise OSError("disk full")

    llm = record(path)
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        await llm.chat_json(**{**CALL, "user": "another"})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


async def test_recording_adds_to_existing_cassette(tmp_path):
    path = tmp_path / "c.jsonl"
    await record(path).chat_json(**CALL)
    await record(path).chat_json(**{**CALL, "user": "second"})
    replay = CassetteLLM(path, "replay")
    await replay.chat_json(**CALL)
    await replay.chat_json(**{**CALL, "user": "second"})


# B2 ------------------------------------------------------------------------
def test_b2_guards_reject_non_fixture_hosts():
    ok = "postgresql+asyncpg://u:p@app-postgres.nn-eval.test:5432/db"
    guards.assert_fixture_hosts(ok)
    with pytest.raises(guards.FixtureGuardError):
        guards.assert_fixture_hosts("postgresql+asyncpg://u:p@localhost:5433/db")
    with pytest.raises(guards.FixtureGuardError):
        guards.assert_fixture_hosts("postgresql+asyncpg://u:p@db.supabase.co:5432/postgres")
    with pytest.raises(guards.FixtureGuardError):
        guards.assert_fixture_hosts(ok, client_host="db.customer.example.com")
    with pytest.raises(guards.FixtureGuardError):
        guards.assert_fixture_hosts(None)


def test_b2_guard_errors_do_not_leak_credentials():
    with pytest.raises(guards.FixtureGuardError) as exc:
        guards.assert_fixture_hosts("postgresql+asyncpg://user:SuperSecretPw@evil.example.com/db")
    assert "SuperSecretPw" not in str(exc.value)
