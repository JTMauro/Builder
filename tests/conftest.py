import subprocess

import pytest

from media_backend import config, jobs, storage


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_BACKEND_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("MEDIA_BACKEND_ALLOWED_DIRS", str(tmp_path))
    monkeypatch.delenv("MEDIA_BACKEND_TOKEN", raising=False)
    config.reset_settings()
    jobs._queue = None
    yield tmp_path
    config.reset_settings()


@pytest.fixture
def media(tmp_path):
    """Generate a 3s test video (with tone), a 2s tone, and a small PNG 'character'."""
    v, a, img, img2 = tmp_path / "clip.mp4", tmp_path / "tone.wav", tmp_path / "hero.png", tmp_path / "hero2.png"
    _ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(v))
    _ff("-f", "lavfi", "-i", "sine=f=660:d=2", str(a))
    _ff("-f", "lavfi", "-i", "color=c=orange:s=200x300", "-frames:v", "1", str(img))
    _ff("-f", "lavfi", "-i", "color=c=steelblue:s=200x300", "-frames:v", "1", str(img2))
    return {
        "video": storage.import_path(v).id,
        "audio": storage.import_path(a).id,
        "image": storage.import_path(img).id,
        "image2": storage.import_path(img2).id,
        "dir": tmp_path,
    }
