"""`builder bench`: the steps, the dated reports, comparison with the last run, cleanup and the (off by default) schedule."""
import datetime as dt
import subprocess
import threading
import time

import pytest

from media_backend import bench, cli, storage
from media_backend.ops import OpResult


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def fake_ops(monkeypatch, tmp_path):
    """Stand-in operations: a scene video made by ffmpeg, canned check results. state[] tweaks the next run."""
    state = {"review_issues": [], "lpips": 0.02, "calls": [], "fail": set()}
    old = tmp_path / "old.png"
    _ff("-f", "lavfi", "-i", "color=c=gray:s=32x32", "-frames:v", "1", str(old))
    state["old_id"] = storage.import_path(old).id  # made before the bench: must survive cleanup

    def op(name, params):
        state["calls"].append((name, params))
        if name in state["fail"]:
            raise RuntimeError(f"{name} broke")
        if name == "scene_video":
            voice = tmp_path / f"voice{len(state['calls'])}.wav"
            _ff("-f", "lavfi", "-i", "sine=f=300:d=1", str(voice))
            vid = storage.import_path(voice).id
            mouth = storage.import_path(voice)  # made from the voice line but not linked to the output (lip-sync cues)
            mm = storage.get(mouth.id).model_copy(update={"parent_ids": [vid]})
            (storage.path_of(mouth.id).parent / "meta.json").write_text(mm.model_dump_json())
            out = tmp_path / "scene_teen_640x360.mp4"
            _ff("-f", "lavfi", "-i", "testsrc=size=160x90:rate=12:duration=2", "-f", "lavfi", "-i", "sine=f=440:d=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out))
            info = storage.import_path(out)
            meta = storage.get(info.id).model_copy(update={"parent_ids": [vid, state["old_id"]]})
            (storage.path_of(info.id).parent / "meta.json").write_text(meta.model_dump_json())
            return OpResult(outputs=[storage.get(info.id)], data={"duration_s": 2.0})
        if name == "audio_check":
            return OpResult(outputs=[], data={"ok": True, "issues": [], "lufs": -16.2, "true_peak_db": -2.0,
                                              "speech": {"word_accuracy": 1.0}})
        if name == "review_video":
            return OpResult(outputs=[], data={"ok": not state["review_issues"], "issues": list(state["review_issues"]),
                                              "motion": {"mean_pct": 1.0}, "jitter": {"score": 0.1}, "sharpness": {"median": 80}})
        if name == "style_check":
            return OpResult(outputs=[], data={"pass": True, "views": {"face": {"ssim": 0.99, "pass": True}}})
        if name == "quality_score":
            d = {"aesthetic": {"mean": 5.1}}
            if params.get("reference_id"):
                d["lpips"] = {"mean": state["lpips"]}
            return OpResult(outputs=[], data=d)
        raise AssertionError(name)

    monkeypatch.setattr(bench, "_op", op)
    monkeypatch.setattr(bench, "_stage_ready", lambda: None)
    monkeypatch.setattr(bench, "_voices_ready", lambda: True)
    monkeypatch.setattr(bench, "_senses_ready", lambda: None)
    return state


def _steps(r):
    return {k: v["status"] for k, v in r["steps"].items()}


def test_bench_runs_writes_reports_and_cleans_up(fake_ops):
    r = bench.run_bench(log=lambda m: None)
    assert _steps(r) == {"scene": "ok", "audio_check": "ok", "review_video": "ok", "style_check": "skipped", "quality": "ok"}
    assert "approve" in r["steps"]["style_check"]["reason"]
    assert r["ok"] and r["changes"] == ["first run: nothing to compare with"]
    root = bench.bench_dir()
    assert (root / f"{r['stamp']}.json").exists() and (root / f"{r['stamp']}.html").exists()
    assert (root / r["stamp"] / "scene.mp4").exists() and (root / r["stamp"] / "frame.png").exists()
    params = dict(fake_ops["calls"])["scene_video"]
    assert params["resolution"] == "640x360" and params["fps"] == 12 and params["quality"] == "fast"
    assert dict(fake_ops["calls"])["audio_check"].get("expected_text", bench.LINE) == bench.LINE
    # the bench's own files (and their new parents) are gone, older files stay
    ids = {f.id for f in storage.list_files()}
    assert ids == {fake_ops["old_id"]}
    html = (root / f"{r['stamp']}.html").read_text()
    assert "<title>Builder bench" in html and "scene.mp4" in html


def test_bench_flags_regressions_against_previous_run(fake_ops):
    first = bench.run_bench(log=lambda m: None)
    time.sleep(1.1)  # stamps have one-second resolution
    fake_ops["review_issues"] = ["Back-and-forth jitter in second(s) 1"]
    fake_ops["lpips"] = 0.4
    fake_ops["fail"] = {"audio_check"}
    second = bench.run_bench(log=lambda m: None)
    assert second["compared_with"] == first["stamp"] and not second["ok"]
    regs = " | ".join(second["regressions"])
    assert "audio_check failed" in regs and "jitter" in regs and "LPIPS 0.400" in regs
    qp = [p for n, p in fake_ops["calls"] if n == "quality_score"][-1]
    assert "reference_id" in qp  # compared with the previous run's frame
    assert bench.latest_report()["stamp"] == second["stamp"]


def test_bench_approve_and_skips(fake_ops, monkeypatch):
    r = bench.run_bench(approve=True, log=lambda m: None)
    assert r["steps"]["style_check"]["status"] == "ok" and r["steps"]["style_check"]["approved_now"]
    assert dict(fake_ops["calls"])["style_check"]["approve"] is True
    time.sleep(1.1)
    monkeypatch.setattr(bench, "_stage_ready", lambda: "no 'teen' stage")
    r = bench.run_bench(log=lambda m: None)
    assert _steps(r) == {"scene": "skipped", "audio_check": "skipped", "review_video": "skipped", "style_check": "skipped", "quality": "skipped"}
    assert any("scene: ok -> skipped" in c for c in r["changes"])


def test_bench_refuses_to_overlap(fake_ops):
    (bench.bench_dir() / ".running").write_text("123")
    with pytest.raises(RuntimeError, match="another bench"):
        bench.run_bench(log=lambda m: None)


def test_cli_bench_exit_codes(fake_ops, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["bench"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "✓ scene ok" in out and "Report:" in out


def test_compare_slow_and_style():
    prev = {"steps": {"scene": {"status": "ok", "seconds": 40}, "style_check": {"status": "ok", "pass": True}}}
    cur = {"steps": {"scene": {"status": "ok", "seconds": 100}, "style_check": {"status": "ok", "pass": False}}}
    regs, changes = bench.compare(prev, cur)
    assert any("took 100 s" in c for c in changes) and any("no longer matches" in r for r in regs)


def test_schedule_off_by_default_and_next_run(monkeypatch):
    monkeypatch.delenv("BUILDER_BENCH", raising=False)
    assert bench.ensure_scheduler() is False
    now = dt.datetime(2026, 11, 23, 2, 30)
    assert bench.next_run(now, 3) == dt.datetime(2026, 11, 23, 3, 0)
    assert bench.next_run(now.replace(hour=4), 3) == dt.datetime(2026, 11, 24, 3, 0)
    monkeypatch.setenv("BUILDER_BENCH_HOUR", "99")
    assert bench.bench_hour() == 23
    assert any("schedule off" in text for _, text in bench.doctor_lines())


def test_schedule_loop_waits_for_jobs(monkeypatch):
    monkeypatch.setattr(bench, "next_run", lambda now, hour: now + dt.timedelta(seconds=0.05))
    stop = threading.Event()
    busy = iter([True, False, False])
    ran = []

    def run():
        ran.append(time.time())
        stop.set()

    waits = []
    real_wait = stop.wait
    monkeypatch.setattr(stop, "wait", lambda s: waits.append(s) or real_wait(min(s, 0.05)))
    t = threading.Thread(target=bench._loop, args=(stop, run, lambda: next(busy, False)))
    t.start()
    t.join(5)
    assert ran and 300 in waits  # waited for the running job before the bench
