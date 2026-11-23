"""Nightly self-test (`builder bench`): render a short fixed scene and check it with Builder's own tools, no Claude.

Steps (each skipped with a reason when its tools are missing):
  scene        scene_video on the teen stage, grey studio set, 640x360, 12 fps, ~4 s, quality fast
  audio_check  loudness / peaks / clipping, and the spoken line against the script (Whisper) when voices exist
  review_video motion, jitter, frozen / black frames, sharpness
  style_check  the character against its approved pictures (`builder bench --approve` saves them)
  quality      LPIPS of the middle frame against the previous run's, plus the aesthetic score (builder[senses])

Each run writes <data>/bench/<stamp>.json and .html (and a folder with the video and key pictures), then compares
with the previous run and lists regressions. Files the bench made in the file store are removed afterwards.

The in-server nightly schedule is OFF unless BUILDER_BENCH=1; BUILDER_BENCH_HOUR (0-23, local time, default 3)
picks the hour. It runs `builder bench --nice` as a low-priority child process and waits while jobs are running.
"""

from __future__ import annotations

import datetime as dt
import html
import importlib.util
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import get_settings

STAGE = "teen"
LINE = "Hello there! This is the nightly test of the studio."
KEEP_RUNS = 60


def bench_dir() -> Path:
    d = Path(os.environ.get("BUILDER_BENCH_DIR") or (get_settings().data_dir / "bench")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _op(name: str, params: dict[str, Any]):
    """Run one Builder operation (tests replace this)."""
    from . import ops

    return ops.run_operation(name, params)


def scene_params(voiced: bool) -> dict[str, Any]:
    beat: dict[str, Any] = {"move": "wave", "view": "body"}
    if voiced:
        beat["say"] = LINE
    else:
        beat["hold"] = 3.5
    return {"stage": STAGE, "set": None, "resolution": "640x360", "fps": 12, "quality": "fast", "captions": False,
            "look": "none", "music": "none", "footsteps": False, "tail": 0.5, "beats": [beat]}


def _voices_ready() -> bool:
    from .voice import models_dir

    return _has("kokoro_onnx") and (models_dir() / "kokoro" / "model.onnx").exists()


def _stage_ready() -> str | None:
    """None when scenes can render, else the reason they can't."""
    from . import scene

    if os.environ.get("SCENE_RENDERER", "").endswith(".py"):
        return None if (scene.stages_dir() / STAGE / "index.html").exists() else f"no '{STAGE}' stage in {scene.stages_dir()}"
    if not (scene.stages_dir() / STAGE / "index.html").exists():
        return f"no '{STAGE}' stage in {scene.stages_dir()} (tools/setup_studio.sh makes one)"
    if not shutil.which("node"):
        return "Node.js is not installed (needed to render scenes)"
    return None


def _senses_ready() -> str | None:
    if not (_has("torch") and _has("transformers") and _has("lpips")):
        return 'needs pip install "builder[senses]"'
    from . import senses

    if not senses.status()["clip"]:
        return "the scoring models aren't downloaded yet (run quality_score once)"
    return None


def _step(name: str, steps: dict[str, Any], fn: Callable[[], dict[str, Any] | str]) -> None:
    t0 = time.time()
    try:
        out = fn()
        if isinstance(out, str):
            steps[name] = {"status": "skipped", "reason": out}
        else:
            steps[name] = {"status": "ok", "seconds": round(time.time() - t0, 1), **out}
    except Exception as e:  # noqa: BLE001 - one broken tool must not stop the rest of the bench
        steps[name] = {"status": "failed", "seconds": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}"[:800]}


def _frame(video: Path, at: float, out: Path) -> None:
    subprocess.run([get_settings().ffmpeg, "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", str(out)],
                   check=True, capture_output=True)


def run_bench(approve: bool = False, keep_files: bool = False, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Run every step, write the dated JSON + HTML report, compare with the previous run. Returns the report."""
    from . import storage

    root = bench_dir()
    lock = root / ".running"
    if lock.exists() and time.time() - lock.stat().st_mtime < 3 * 3600:
        raise RuntimeError(f"another bench is running (remove {lock} if not)")
    lock.write_text(str(os.getpid()), encoding="utf-8")
    started = time.time()
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = root / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    prev = latest_report(before=stamp)
    steps: dict[str, Any] = {}
    made: list[str] = []
    ctx: dict[str, Any] = {}
    try:
        stage_problem = _stage_ready()
        voiced = _voices_ready()

        def scene():
            if stage_problem:
                return stage_problem
            r = _op("scene_video", scene_params(voiced))
            made.extend(o.id for o in r.outputs)
            vid = next(o for o in r.outputs if o.name.endswith(".mp4"))
            ctx["video_id"], ctx["video"] = vid.id, storage.path_of(vid.id)
            shutil.copy(ctx["video"], run_dir / "scene.mp4")
            return {"file": "scene.mp4", "voiced": voiced, "duration_s": r.data.get("duration_s") or r.data.get("duration")}

        log("· scene_video …")
        _step("scene", steps, scene)

        def audio():
            if "video_id" not in ctx:
                return "no scene video"
            params: dict[str, Any] = {"file_id": ctx["video_id"]}
            if voiced and _has("faster_whisper"):
                params["expected_text"] = LINE
            r = _op("audio_check", params)
            made.extend(o.id for o in r.outputs)
            d = r.data
            return {"ok": d.get("ok"), "issues": d.get("issues", []), "lufs": d.get("lufs"), "true_peak_db": d.get("true_peak_db"),
                    "word_accuracy": (d.get("speech") or {}).get("word_accuracy")}

        log("· audio_check …")
        _step("audio_check", steps, audio)

        def review():
            if "video_id" not in ctx:
                return "no scene video"
            r = _op("review_video", {"file_id": ctx["video_id"]})
            made.extend(o.id for o in r.outputs)
            d = r.data
            sheet = next((o for o in r.outputs if o.name.endswith("_contact_sheet.png")), None)
            if sheet:
                shutil.copy(storage.path_of(sheet.id), run_dir / "contact_sheet.png")
            return {"ok": d.get("ok"), "issues": d.get("issues", []), "motion_pct": (d.get("motion") or {}).get("mean_pct"),
                    "jitter": (d.get("jitter") or {}).get("ratio"), "sharpness": (d.get("sharpness") or {}).get("median")}

        log("· review_video …")
        _step("review_video", steps, review)

        def style():
            from . import director

            if stage_problem:
                return stage_problem
            if not approve and not director.approved_dir(STAGE).exists():
                return "nothing approved yet (run `builder bench --approve` once the character looks right)"
            r = _op("style_check", {"stage": STAGE, "approve": approve})
            made.extend(o.id for o in r.outputs)
            views = {k: (v if isinstance(v, str) else {"ssim": v.get("ssim"), "pass": v.get("pass")}) for k, v in r.data.get("views", {}).items()}
            return {"pass": r.data.get("pass"), "approved_now": approve, "views": views}

        log("· style_check …")
        _step("style_check", steps, style)

        def quality():
            if "video" not in ctx:
                return "no scene video"
            problem = _senses_ready()
            if problem:
                return problem
            dur = float((steps["scene"].get("duration_s") or 3.0))
            frame = run_dir / "frame.png"
            _frame(ctx["video"], max(0.0, dur / 2), frame)
            fid = storage.import_path(frame, source="bench").id
            made.append(fid)
            params: dict[str, Any] = {"file_id": fid}
            ref = (root / prev["stamp"] / "frame.png") if prev else None
            if ref and ref.exists():
                rid = storage.import_path(ref, source="bench").id
                made.append(rid)
                params["reference_id"] = rid
            r = _op("quality_score", params)
            made.extend(o.id for o in r.outputs)
            d = r.data
            return {"lpips_vs_previous": (d.get("lpips") or {}).get("mean"), "aesthetic": (d.get("aesthetic") or {}).get("mean"),
                    "compared_with": prev["stamp"] if params.get("reference_id") else None}

        log("· quality_score …")
        _step("quality", steps, quality)
    finally:
        if not keep_files:
            _cleanup(made, started)
        lock.unlink(missing_ok=True)
    report = {"stamp": stamp, "started": dt.datetime.fromtimestamp(started).isoformat(timespec="seconds"),
              "seconds": round(time.time() - started, 1), "steps": steps}
    report["compared_with"] = prev["stamp"] if prev else None
    report["regressions"], report["changes"] = compare(prev, report)
    report["ok"] = not report["regressions"] and not any(s["status"] == "failed" for s in steps.values())
    (root / f"{stamp}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (root / f"{stamp}.html").write_text(render_html(report), encoding="utf-8")
    _prune(root)
    return report


def _cleanup(ids: list[str], since: float) -> None:
    """Delete what this bench made in the file store, including intermediate files (voices, frames) it created."""
    from . import storage

    seen: set[str] = set()
    gone: set[str] = set()
    todo = list(ids)
    while todo:
        fid = todo.pop()
        if fid in seen:
            continue
        seen.add(fid)
        try:
            info = storage.get(fid)
        except Exception:  # noqa: BLE001 - already gone
            continue
        if info.created_at < since:
            continue  # older than the bench: never touch
        todo.extend(info.parent_ids)
        try:
            storage.delete(fid)
            gone.add(fid)
        except Exception:  # noqa: BLE001
            pass
    # files made from those intermediates but not linked to an output (e.g. lip-sync cues of a bench voice line)
    changed = True
    while changed:
        changed = False
        for f in storage.list_files():
            if f.created_at >= since and gone.intersection(f.parent_ids):
                try:
                    storage.delete(f.id)
                    gone.add(f.id)
                    changed = True
                except Exception:  # noqa: BLE001
                    pass


def _prune(root: Path) -> None:
    runs = sorted(root.glob("*.json"))
    for old in runs[:-KEEP_RUNS]:
        for p in (old, old.with_suffix(".html")):
            p.unlink(missing_ok=True)
        shutil.rmtree(root / old.stem, ignore_errors=True)


def latest_report(before: str | None = None) -> dict[str, Any] | None:
    root = bench_dir()
    for f in sorted(root.glob("*.json"), reverse=True):
        if before and f.stem >= before:
            continue
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def compare(prev: dict[str, Any] | None, cur: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(regressions, other changes) between two reports."""
    reg: list[str] = []
    chg: list[str] = []
    if not prev:
        return reg, ["first run: nothing to compare with"]
    ps, cs = prev.get("steps", {}), cur.get("steps", {})
    for name, c in cs.items():
        p = ps.get(name) or {}
        if c["status"] == "failed" and p.get("status") != "failed":
            reg.append(f"{name} failed (was {p.get('status', 'missing')}): {c.get('error', '')[:200]}")
        elif c["status"] != p.get("status"):
            chg.append(f"{name}: {p.get('status', 'missing')} -> {c['status']}")
        if c["status"] == "ok" and p.get("status") == "ok" and p.get("seconds") and c.get("seconds", 0) > 1.5 * p["seconds"] + 20:
            # timing is noisy on a shared CPU: a note, not a regression
            chg.append(f"{name} took {c['seconds']:.0f} s (was {p['seconds']:.0f} s)")
    a, pa = cs.get("audio_check", {}), ps.get("audio_check", {})
    if a.get("status") == pa.get("status") == "ok":
        for issue in sorted(set(a.get("issues", [])) - set(pa.get("issues", []))):
            reg.append(f"audio: {issue}")
        if a.get("word_accuracy") is not None and pa.get("word_accuracy") is not None and a["word_accuracy"] < pa["word_accuracy"] - 0.05:
            reg.append(f"speech matches the script less: {pa['word_accuracy']:.0%} -> {a['word_accuracy']:.0%}")
        if a.get("lufs") is not None and pa.get("lufs") is not None and abs(a["lufs"] - pa["lufs"]) > 2:
            chg.append(f"loudness moved {pa['lufs']} -> {a['lufs']} LUFS")
    r, pr = cs.get("review_video", {}), ps.get("review_video", {})
    if r.get("status") == pr.get("status") == "ok":
        for issue in sorted(set(r.get("issues", [])) - set(pr.get("issues", []))):
            reg.append(f"picture: {issue}")
        for issue in sorted(set(pr.get("issues", [])) - set(r.get("issues", []))):
            chg.append(f"fixed: {issue}")
    s, pst = cs.get("style_check", {}), ps.get("style_check", {})
    if s.get("status") == "ok" and not s.get("approved_now") and s.get("pass") is False and pst.get("pass") is not False:
        reg.append("style_check: the character no longer matches the approved pictures")
    q, pq = cs.get("quality", {}), ps.get("quality", {})
    if q.get("status") == "ok":
        if q.get("lpips_vs_previous") is not None and q["lpips_vs_previous"] > 0.15:
            reg.append(f"the render looks different from last run (LPIPS {q['lpips_vs_previous']:.3f}; over 0.15)")
        if q.get("aesthetic") is not None and pq.get("aesthetic") is not None and q["aesthetic"] < pq["aesthetic"] - 0.4:
            reg.append(f"aesthetic score dropped {pq['aesthetic']:.2f} -> {q['aesthetic']:.2f}")
    return reg, chg


def render_html(r: dict[str, Any]) -> str:
    e = html.escape
    rows = []
    for name, s in r["steps"].items():
        detail = s.get("reason") or s.get("error") or ", ".join(f"{k}: {v}" for k, v in s.items() if k not in ("status", "seconds"))
        rows.append(f"<tr class='{e(s['status'])}'><td>{e(name)}</td><td>{e(s['status'])}</td>"
                    f"<td>{e(str(s.get('seconds', '')))}</td><td>{e(str(detail))[:900]}</td></tr>")
    pics = "".join(f"<figure><img src='{e(r['stamp'])}/{n}' alt=''><figcaption>{n}</figcaption></figure>"
                   for n in ("frame.png", "contact_sheet.png"))
    reg = "".join(f"<li>{e(x)}</li>" for x in r["regressions"]) or "<li>none</li>"
    chg = "".join(f"<li>{e(x)}</li>" for x in r["changes"]) or "<li>none</li>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Builder bench {e(r['stamp'])}</title><style>
:root{{color-scheme:light dark;--bg:#fafafa;--fg:#222;--bad:#b3261e;--ok:#1e7b34;--muted:#777}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e6e6e6;--bad:#ff8a80;--ok:#7ed69a;--muted:#999}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:960px;padding:16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #8884;padding:6px;text-align:left;vertical-align:top}}
.failed td:nth-child(2){{color:var(--bad);font-weight:600}}.ok td:nth-child(2){{color:var(--ok)}}.skipped td:nth-child(2){{color:var(--muted)}}
img{{max-width:100%}}figure{{margin:12px 0}}video{{max-width:100%}}
</style></head><body><h1>Builder bench: {e(r['stamp'])}</h1>
<p>{'All good' if r['ok'] else 'Needs a look'} · {r['seconds']} s · compared with {e(str(r.get('compared_with') or 'nothing (first run)'))}</p>
<h2>Regressions</h2><ul>{reg}</ul><h2>Other changes</h2><ul>{chg}</ul>
<h2>Steps</h2><table><tr><th>Step</th><th>Status</th><th>Seconds</th><th>Details</th></tr>{''.join(rows)}</table>
<h2>Pictures</h2><video controls src="{e(r['stamp'])}/scene.mp4"></video>{pics}</body></html>"""


# ---------------------------------------------------------------- optional nightly schedule (off by default)

_sched: dict[str, Any] = {"thread": None, "stop": None}


def schedule_enabled() -> bool:
    return os.environ.get("BUILDER_BENCH", "").strip().lower() in ("1", "true", "yes", "on")


def bench_hour() -> int:
    try:
        return min(23, max(0, int(os.environ.get("BUILDER_BENCH_HOUR", "3"))))
    except ValueError:
        return 3


def next_run(now: dt.datetime, hour: int) -> dt.datetime:
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return nxt if nxt > now else nxt + dt.timedelta(days=1)


def _busy() -> bool:
    """True while a Builder job is queued or running (the bench must not compete with real work)."""
    from . import jobs

    q = jobs._queue
    return bool(q) and any(j.status in ("queued", "running") for j in q.list(200))


def _run_child() -> None:
    from .config import builder_command

    kw: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "posix":
        kw["preexec_fn"] = lambda: os.nice(10)
    subprocess.run(builder_command("bench", "--nice"), timeout=3600, **kw)


def _loop(stop: threading.Event, run: Callable[[], None] = _run_child, busy: Callable[[], bool] = _busy) -> None:
    while not stop.is_set():
        wait = (next_run(dt.datetime.now(), bench_hour()) - dt.datetime.now()).total_seconds()
        if stop.wait(max(1.0, wait)):
            return
        deadline = time.time() + 3 * 3600  # give real work up to three hours to finish, else skip tonight
        while busy() and time.time() < deadline:
            if stop.wait(300):
                return
        if not busy():
            try:
                run()
            except Exception:  # noqa: BLE001 - a failed night must not kill the scheduler
                pass


def ensure_scheduler() -> bool:
    """Start the nightly thread when BUILDER_BENCH=1 (called from the server's startup). Returns whether it runs."""
    if not schedule_enabled():
        return False
    t = _sched["thread"]
    if t is not None and t.is_alive():
        return True
    stop = threading.Event()
    t = threading.Thread(target=_loop, args=(stop,), name="builder-bench", daemon=True)
    _sched.update(thread=t, stop=stop)
    t.start()
    return True


def stop_scheduler() -> None:
    if _sched["stop"] is not None:
        _sched["stop"].set()
    _sched.update(thread=None, stop=None)


def doctor_lines() -> list[tuple[bool, str]]:
    last = latest_report()
    sched = f"on, nightly at {bench_hour():02d}:00 (BUILDER_BENCH_HOUR)" if schedule_enabled() else "off (BUILDER_BENCH=1 turns on the nightly run in the server)"
    lines = [(True, f"self-test bench: `builder bench`; schedule {sched}")]
    if last:
        n = len(last.get("regressions", []))
        lines.append((bool(last.get("ok")), f"last bench {last['stamp']}: " + ("all good" if last.get("ok") else f"{n} regression(s), see {bench_dir() / (last['stamp'] + '.html')}")))
    return lines
