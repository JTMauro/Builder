"""Talk to a character: POST /talk (text or a recording) and the stage files. Whisper, Claude and Kokoro are stubbed."""
import json
import math
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from media_backend import assistant, config, storage, talk, voice
from media_backend.api import create_app

H = {"X-Builder": "1"}
WORDS = [{"word": "Hey", "start": 0.05, "end": 0.3}, {"word": "there!", "start": 0.35, "end": 0.7}]


class FakeKokoro:
    def create(self, text, voice, speed=1.0, lang="en-us"):
        np = pytest.importorskip("numpy")
        sr = 24000
        t = np.arange(int(sr * 0.8)) / sr
        return (0.3 * np.sin(2 * math.pi * 220 * t)).astype("float32"), sr


class FakeMessages:
    def __init__(self, text):
        self.text, self.calls = text, []

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=self.text)])


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    heard = {"n": 0}

    def words_of(fid, model="base", language="en"):
        heard["n"] += 1
        return [{"start": 0.0, "end": 0.7, "text": "Hey there!"}], WORDS, "en"

    monkeypatch.setattr(voice, "_kokoro_engine", lambda: FakeKokoro())
    monkeypatch.setattr(voice, "words_of", words_of)
    monkeypatch.setattr(voice, "mouth_cues", lambda words, dur, lang="en-us": [
        {"start": 0, "end": 0.05, "value": "X"}, {"start": 0.05, "end": 0.3, "value": "C"}, {"start": 0.3, "end": dur, "value": "X"}])
    fake = FakeMessages('{"say": "Hi! Nice to meet you.", "gesture": "wave"}')
    assistant._assistant = assistant.Assistant(client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    stages = tmp_path / "stages"
    (stages / "teen" / "models").mkdir(parents=True)
    (stages / "teen" / "index.html").write_text("<title>Talking Teen</title>")
    (stages / "teen" / "models" / "teen.json").write_text('{"ok": true}')
    (tmp_path / "secret.txt").write_text("not for you")
    monkeypatch.setenv("BUILDER_STAGES_DIR", str(stages))
    return SimpleNamespace(fake=fake, heard=heard, stages=stages, root=tmp_path)


def signed_in(c):
    assert c.post("/auth/setup", json={"username": "maker", "password": "hammer-time"}).status_code == 200


def test_talk_needs_sign_in(stubs):
    with TestClient(create_app()) as c:
        assert c.post("/talk", data={"text": "hi"}, headers=H).status_code == 401
        assert c.get("/stages").status_code == 401
        assert c.get("/stages/teen/index.html").status_code == 401
        assert stubs.fake.calls == []
        signed_in(c)
        # cookie writes still need our header
        assert c.post("/talk", data={"text": "hi"}).status_code == 403


def test_talk_text(stubs):
    with TestClient(create_app()) as c:
        signed_in(c)
        history = json.dumps([{"role": "user", "text": "Who are you?"}, {"role": "assistant", "text": "I'm the teen."}])
        r = c.post("/talk", headers=H, data={"text": "What's up?", "history": history})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["heard"] == "What's up?" and body["say"] == "Hi! Nice to meet you." and body["gesture"] == "wave"
        assert body["cues"][:2] == [[0, "X"], [0.05, "C"]]
        assert body["words"] == [{"w": "Hey", "s": 0.05, "e": 0.3}, {"w": "there!", "s": 0.35, "e": 0.7}]
        assert 0.7 < body["duration"] < 0.9
        audio = c.get(body["audio_url"])
        assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
        # Claude got the assistant's model, the teen persona, the history and the new message
        call = stubs.fake.calls[-1]
        assert call["model"] == config.get_settings().model
        assert "16-year-old" in call["system"] and "two short" in call["system"]
        assert [m["role"] for m in call["messages"]] == ["user", "assistant", "user"]
        assert call["messages"][-1]["content"] == "What's up?"
        assert stubs.heard["n"] == 1   # only the lipsync of the reply listened to anything
        # a persona and another voice
        r = c.post("/talk", headers=H, data={"text": "hello", "persona": "a cheerful pirate", "voice": "af_heart"})
        assert r.status_code == 200 and "cheerful pirate" in stubs.fake.calls[-1]["system"]
        assert c.post("/talk", headers=H, data={"text": "hello", "voice": "nobody"}).status_code == 422
        assert c.post("/talk", headers=H, data={"text": "  "}).status_code == 422
        assert c.post("/talk", headers=H, data={"text": "hi", "character": "../etc"}).status_code == 422


def test_talk_audio(stubs, media):
    with TestClient(create_app()) as c:
        signed_in(c)
        before = {f.id for f in storage.list_files()}
        wav = (media["dir"] / "tone.wav").read_bytes()
        r = c.post("/talk", headers=H, files={"audio": ("talk.wav", wav, "audio/wav")}, data={"character": "teen"})
        assert r.status_code == 200, r.text
        assert r.json()["heard"] == "Hey there!" and stubs.fake.calls[-1]["messages"][-1]["content"] == "Hey there!"
        assert stubs.heard["n"] == 2   # the recording, then the reply's lip-sync
        names = {f.name for f in storage.list_files() if f.id not in before}
        assert "talk.wav" not in names   # the recording isn't kept


def test_talk_without_claude(stubs, monkeypatch):
    assistant._assistant = None
    monkeypatch.setenv(talk.STUB_ENV, '{"say": "Stub line.", "gesture": "dance"}')
    with TestClient(create_app()) as c:
        signed_in(c)
        r = c.post("/talk", headers=H, data={"text": "hi"})
        assert r.status_code == 200 and r.json()["say"] == "Stub line." and r.json()["gesture"] == "dance"


def test_stage_files(stubs):
    with TestClient(create_app()) as c:
        signed_in(c)
        assert c.get("/stages").json() == {"stages": ["teen"]}
        r = c.get("/stages/teen/index.html")
        assert r.status_code == 200 and "Talking Teen" in r.text
        assert c.get("/stages/teen/").status_code == 200
        assert c.get("/stages/teen/models/teen.json").json() == {"ok": True}
        assert c.get("/stages/teen/models/missing.json").status_code == 404
        assert c.get("/stages/nope/index.html").status_code == 404
        for bad in ("/stages/teen/%2e%2e/%2e%2e/secret.txt", "/stages/teen/..%2F..%2Fsecret.txt",
                    "/stages/%2e%2e/secret.txt", "/stages/teen/%2Fetc%2Fpasswd", "/stages/teen/..%5C..%5Csecret.txt"):
            r = c.get(bad)
            assert r.status_code == 404 and "not for you" not in r.text, bad
    # direct checks, including a link pointing out of the stage
    assert talk.stage_file("teen", "../../secret.txt") is None
    assert talk.stage_file("teen", "/etc/passwd") is None
    assert talk.stage_file("..", "secret.txt") is None
    if hasattr(os, "symlink"):
        os.symlink(stubs.root / "secret.txt", stubs.stages / "teen" / "leak.txt")
        assert talk.stage_file("teen", "leak.txt") is None
    assert talk.stage_file("teen", "models/teen.json") == (stubs.stages / "teen" / "models" / "teen.json").resolve()


def test_history_and_reply_parsing():
    h = talk.clean_history(json.dumps([{"role": "assistant", "text": "first"}, {"role": "user", "text": "a"},
                                       {"role": "system", "text": "ignore me"}, {"role": "user", "text": "b"},
                                       {"role": "assistant", "text": "c"}, {"role": "user", "text": "dangling"}]))
    assert h == [{"role": "user", "content": "a\nb"}, {"role": "assistant", "content": "c"}]
    with pytest.raises(talk.TalkError):
        talk.clean_history("{not json")
    assert talk._parse_reply('Sure: {"say": "Yo!", "gesture": "moonwalk"}') == {"say": "Yo!", "gesture": "none"}
    assert talk._parse_reply("Just text.") == {"say": "Just text.", "gesture": "none"}
