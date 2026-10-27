"""HDMI capture of the console's real video output through a USB capture card (DirectShow + ffmpeg).

Complements `XboxConsole.screenshot_raw` (xbdm framebuffer): this sees exactly what the TV shows
(scaler, overlays, any title, dashboard), records video with sound, and doesn't touch the console.

ffmpeg comes from (in order) XBOX360_FFMPEG, `ffmpeg` on PATH, or the binary bundled with the
imageio-ffmpeg package. Devices are auto-detected (webcams and virtual cameras are skipped) or set with
XBOX360_CAPTURE_VIDEO / XBOX360_CAPTURE_AUDIO ("none" = no sound).

A capture card can only be opened by one program at a time: while a recording runs, `photo()` returns
the recording's latest frame instead of opening the card again.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

WARMUP_FRAMES = 10  # the first MJPEG frames after opening the card are corrupt or black
MAX_BURST = 120
_NOT_CAPTURE_VIDEO = re.compile(r"webcam|integrated|virtual|\bobs\b|lsvcam|facetime|\bir\b|droidcam|snap camera", re.I)
_PREFERRED_VIDEO = re.compile(r"capture|hdmi|usb video|cam link|elgato|ugreen|ms2109|avermedia", re.I)
_NOT_CAPTURE_AUDIO = re.compile(r"micro|hands-free|headset|casque|stereo mix|mixage", re.I)


class CaptureError(RuntimeError):
    pass


class CaptureBusy(CaptureError):
    """The capture card is already open in another program or session."""


def ffmpeg_exe() -> str:
    exe = os.environ.get("XBOX360_FFMPEG") or shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover - depends on the install
        raise CaptureError("ffmpeg not found: install imageio-ffmpeg or set XBOX360_FFMPEG") from e


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [ffmpeg_exe(), "-hide_banner", *args], capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as e:
        raise CaptureError(f"ffmpeg did not finish within {timeout:.0f} s (no signal on the HDMI input?)") from e


def _errors(stderr: bytes) -> str:
    """ffmpeg's stderr without the MJPEG decoder noise of the warm-up frames."""
    lines = stderr.decode("utf-8", "replace").splitlines()
    keep = [l for l in lines if l.strip() and not re.search(r"mjpeg|Last message repeated|EOI|SOF", l)]
    return "\n".join(keep[-8:])


def check_ffmpeg_error(stderr: bytes, device: str | None = None) -> None:
    text = _errors(stderr)
    if "Could not run graph" in text or "already in use" in text:
        raise CaptureBusy(
            f"capture card {device or ''} is in use by another program or Claude session "
            "(an xbox_capture_record in progress elsewhere, OBS...). Retry when it's free."
        )
    if "Could not find" in text and "device" in text:
        raise CaptureError(f"capture device {device!r} not found (unplugged?). {text}")
    raise CaptureError(f"ffmpeg failed: {text or 'no output'}")


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
def parse_dshow_devices(stderr: str) -> dict[str, list[str]]:
    """Parse `ffmpeg -list_devices true -f dshow -i dummy` output (old and new formats)."""
    out: dict[str, list[str]] = {"video": [], "audio": []}
    section = None
    for line in stderr.splitlines():
        if "DirectShow video devices" in line:
            section = "video"
        elif "DirectShow audio devices" in line:
            section = "audio"
        if "Alternative name" in line:
            continue
        m = re.search(r'"([^"]+)"(?:\s+\((video|audio|none)\))?', line)
        if not m:
            continue
        kind = m.group(2) or section
        if kind in out and m.group(1) not in out[kind]:
            out[kind].append(m.group(1))
    return out


def list_devices() -> dict[str, list[str]]:
    if os.name != "nt":
        raise CaptureError("HDMI capture uses DirectShow and is only implemented on Windows")
    r = _run(["-list_devices", "true", "-f", "dshow", "-i", "dummy"], timeout=20)
    return parse_dshow_devices(r.stderr.decode("utf-8", "replace"))


def pick_video(names: list[str]) -> str:
    cands = [n for n in names if not _NOT_CAPTURE_VIDEO.search(n)]
    if len(cands) > 1:
        cands = [n for n in cands if _PREFERRED_VIDEO.search(n)] or cands
    if len(cands) == 1:
        return cands[0]
    found = ", ".join(names) or "none"
    if not cands:
        raise CaptureError(f"no capture card found (video devices: {found}); set XBOX360_CAPTURE_VIDEO")
    raise CaptureError(f"several possible capture cards ({', '.join(cands)}); set XBOX360_CAPTURE_VIDEO")


def pick_audio(names: list[str]) -> str | None:
    cands = [n for n in names if "usb" in n.lower() and not _NOT_CAPTURE_AUDIO.search(n)]
    return cands[0] if len(cands) == 1 else None


# ---------------------------------------------------------------------------
# Capture card
# ---------------------------------------------------------------------------
@dataclass
class Recording:
    path: Path
    started: float
    max_seconds: float
    audio: bool
    proc: subprocess.Popen
    latest: Path  # frame refreshed twice a second while recording
    stderr: list[bytes] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started


class CaptureCard:
    def __init__(
        self,
        video: str | None = None,
        audio: str | None = None,
        *,
        size: str = "1920x1080",
        fps: int = 30,
        out_dir: str | Path | None = None,
        source: list[str] | None = None,
        audio_source: list[str] | None = None,
    ):
        """`source` / `audio_source` replace the DirectShow devices with any ffmpeg input arguments
        (without -i's value split off, e.g. ["-f", "lavfi", "-i", "testsrc2"]); tests use them."""
        self._video = video
        self._audio = audio  # None = auto, "" = no sound
        self.size = size
        self.fps = fps
        self.out_dir = Path(out_dir or Path.home() / "xbox360-captures")
        self.source = source
        self.audio_source = audio_source
        self.recording: Recording | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "CaptureCard":
        audio = os.environ.get("XBOX360_CAPTURE_AUDIO")
        return cls(
            video=os.environ.get("XBOX360_CAPTURE_VIDEO") or None,
            audio="" if audio and audio.lower() == "none" else audio,
            size=os.environ.get("XBOX360_CAPTURE_SIZE", "1920x1080"),
            fps=int(os.environ.get("XBOX360_CAPTURE_FPS", "30")),
            out_dir=os.environ.get("XBOX360_CAPTURE_DIR") or None,
        )

    # -- devices ---------------------------------------------------------------
    def devices(self) -> dict:
        """Selected video/audio devices (auto-detected once, then cached)."""
        if self.source is None and (self._video is None or self._audio is None):
            found = list_devices()
            if self._video is None:
                self._video = pick_video(found["video"])
            if self._audio is None:
                self._audio = pick_audio(found["audio"]) or ""
        return {"video": self._video or "test source", "audio": self._audio or None, "size": self.size, "fps": self.fps}

    def _input(self, audio: bool, seconds: float | None = None) -> tuple[list[str], str | None]:
        """ffmpeg input arguments and the audio stream to map (None = no sound).
        `seconds` limits every input, so all outputs (recording + live preview) end together."""
        limit = ["-t", f"{seconds:g}"] if seconds else []
        if self.source is not None:
            args = [*limit, *self.source]
            if audio and self.audio_source:
                return [*args, *limit, *self.audio_source], "1:a"
            return args, None
        dev = self.devices()
        spec = f"video={dev['video']}"
        if audio and dev["audio"]:
            spec += f":audio={dev['audio']}"
        args = [
            *limit, "-f", "dshow", "-rtbufsize", "256M", "-vcodec", "mjpeg",
            "-video_size", self.size, "-framerate", str(self.fps), "-i", spec,
        ]
        return args, "0:a" if audio and dev["audio"] else None

    def _new_path(self, prefix: str, ext: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self.out_dir / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}{ext}"

    def _check_free(self) -> None:
        if self.recording and self.recording.running:
            raise CaptureBusy("a recording is running in this session: stop it first (xbox_capture_record stop)")

    # -- photo / burst -----------------------------------------------------------
    def photo(self, path: str | Path | None = None) -> dict:
        """One frame as PNG. During a recording, returns the recording's latest frame (≤0.5 s old)."""
        out = Path(path) if path else self._new_path("photo", ".png")
        out.parent.mkdir(parents=True, exist_ok=True)
        rec = self.recording
        if rec and rec.running:
            if not rec.latest.exists():
                time.sleep(1.0)
            from PIL import Image

            Image.open(rec.latest).save(out)
            return {"path": str(out), "from_recording": str(rec.path)}
        with self._lock:
            r = _run(
                [*self._input(audio=False)[0], "-vf", f"select=gte(n\\,{WARMUP_FRAMES})",
                 "-fps_mode", "passthrough", "-frames:v", "1", "-y", str(out)],
                timeout=30,
            )
        if r.returncode != 0 or not out.exists():
            check_ffmpeg_error(r.stderr, self._video)
        return {"path": str(out)}

    def burst(self, count: int = 8, interval: float = 0.25, out_dir: str | Path | None = None) -> dict:
        """`count` frames `interval` seconds apart (0 = every frame the card delivers), as JPEG files."""
        self._check_free()
        count = max(1, min(int(count), MAX_BURST))
        folder = Path(out_dir) if out_dir else self._new_path("burst", "")
        folder.mkdir(parents=True, exist_ok=True)
        vf = f"select=gte(n\\,{WARMUP_FRAMES})"
        if interval > 0:
            vf += f",fps=1/{interval:g}"
        with self._lock:
            r = _run(
                [*self._input(audio=False)[0], "-vf", vf, "-fps_mode", "passthrough" if interval <= 0 else "cfr",
                 "-frames:v", str(count), "-q:v", "2", "-y", str(folder / "frame_%03d.jpg")],
                timeout=30 + count * max(interval, 0.1),
            )
        frames = sorted(folder.glob("frame_*.jpg"))
        if not frames:
            check_ffmpeg_error(r.stderr, self._video)
        step = interval if interval > 0 else 1 / self.fps
        return {"dir": str(folder), "frames": [str(f) for f in frames], "times": [round(i * step, 3) for i in range(len(frames))]}

    # -- video -------------------------------------------------------------------
    def _record_args(self, path: Path, audio: bool, seconds: float, latest: Path | None) -> tuple[list[str], bool]:
        inputs, audio_map = self._input(audio, seconds)
        args = [*inputs, "-map", "0:v"]
        if audio_map:
            args += ["-map", audio_map, "-c:a", "aac", "-b:a", "160k"]
        args += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            # fragmented MP4: still playable if ffmpeg is killed
            "-movflags", "+frag_keyframe+empty_moov", "-y", str(path),
        ]
        if latest is not None:
            args += ["-map", "0:v", "-vf", "fps=2", "-update", "1", "-q:v", "3", "-y", str(latest)]
        return args, audio_map is not None

    def record(self, seconds: float, path: str | Path | None = None, *, audio: bool = True) -> dict:
        """Record a clip and wait for it (MP4, H.264 + AAC)."""
        self._check_free()
        out = Path(path) if path else self._new_path("video", ".mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        args, has_audio = self._record_args(out, audio, seconds, None)
        with self._lock:
            r = _run(args, timeout=seconds + 30)
        if not out.exists() or out.stat().st_size == 0:
            check_ffmpeg_error(r.stderr, self._video)
        return {"path": str(out), "seconds": round(media_duration(out) or seconds, 2), "audio": has_audio}

    def start_recording(self, path: str | Path | None = None, *, audio: bool = True, max_seconds: float = 600) -> dict:
        """Start recording in the background (stopped by stop_recording or after max_seconds)."""
        self._check_free()
        out = Path(path) if path else self._new_path("video", ".mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        latest = out.with_suffix(".latest.jpg")
        args, has_audio = self._record_args(out, audio, max_seconds, latest)
        proc = subprocess.Popen(
            [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", *args],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rec = Recording(out, time.time(), max_seconds, has_audio, proc, latest)
        threading.Thread(target=lambda: rec.stderr.append(proc.stderr.read()), daemon=True).start()
        time.sleep(1.5)  # opening the card: report a busy/missing device right away
        if proc.poll() not in (None, 0):
            proc.wait()
            time.sleep(0.2)  # let the reader thread collect stderr
            check_ffmpeg_error(b"".join(rec.stderr), self._video)
        self.recording = rec
        return {"recording": str(out), "audio": rec.audio, "max_seconds": max_seconds}

    def stop_recording(self) -> dict:
        rec = self.recording
        if rec is None:
            raise CaptureError("no recording in progress")
        if rec.running:
            try:
                rec.proc.stdin.write(b"q")  # clean stop: ffmpeg finalises the file
                rec.proc.stdin.flush()
            except OSError:
                pass
            try:
                rec.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                rec.proc.kill()
                rec.proc.wait()
        self.recording = None
        rec.latest.unlink(missing_ok=True)
        if not rec.path.exists() or rec.path.stat().st_size == 0:
            check_ffmpeg_error(b"".join(rec.stderr), self._video)
        return {
            "path": str(rec.path),
            "seconds": round(media_duration(rec.path) or rec.elapsed, 2),
            "audio": rec.audio,
            "hit_max_seconds": rec.elapsed >= rec.max_seconds,
        }

    def status(self) -> dict:
        rec = self.recording
        if rec is None:
            return {"recording": False}
        return {"recording": rec.running, "path": str(rec.path), "elapsed_s": round(rec.elapsed, 1), "max_seconds": rec.max_seconds}


# ---------------------------------------------------------------------------
# Previews for Claude: a single labelled contact sheet instead of many images
# ---------------------------------------------------------------------------
def media_duration(path: str | Path) -> float | None:
    r = _run(["-i", str(path)], timeout=20)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr.decode("utf-8", "replace"))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


def sample_video(path: str | Path, count: int = 6, out_dir: str | Path | None = None) -> tuple[list[Path], list[float]]:
    """`count` frames spread evenly over a video file."""
    path = Path(path)
    duration = media_duration(path) or 1.0
    folder = Path(out_dir) if out_dir else path.with_suffix("")
    folder.mkdir(parents=True, exist_ok=True)
    step = duration / count
    r = _run(
        ["-i", str(path), "-vf", f"fps=1/{step:.4f}", "-frames:v", str(count), "-q:v", "3", "-y",
         str(folder / "preview_%02d.jpg")],
        timeout=60,
    )
    frames = sorted(folder.glob("preview_*.jpg"))
    if not frames:
        raise CaptureError(f"could not read {path}: {_errors(r.stderr)}")
    return frames, [round(i * step, 2) for i in range(len(frames))]


def contact_sheet(images: list[str | Path], labels: list[str] | None = None, *, thumb_width: int = 480, cols: int | None = None) -> bytes:
    """Grid of thumbnails with a label on each, as JPEG bytes."""
    import io
    import math

    from PIL import Image, ImageDraw, ImageFont

    thumbs = []
    for p in images:
        im = Image.open(p).convert("RGB")
        im = im.resize((thumb_width, round(im.height * thumb_width / im.width)))
        thumbs.append(im)
    if not thumbs:
        raise CaptureError("no image to put on the sheet")
    cols = cols or min(4, len(thumbs))
    rows = math.ceil(len(thumbs) / cols)
    tw, th = thumbs[0].size
    gap = 4
    sheet = Image.new("RGB", (cols * tw + (cols - 1) * gap, rows * th + (rows - 1) * gap), (20, 20, 20))
    try:
        font = ImageFont.load_default(size=max(14, thumb_width // 20))
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(sheet)
    for i, im in enumerate(thumbs):
        x, y = (i % cols) * (tw + gap), (i // cols) * (th + gap)
        sheet.paste(im, (x, y))
        if labels and i < len(labels):
            box = draw.textbbox((x + 6, y + 4), labels[i], font=font)
            draw.rectangle((box[0] - 4, box[1] - 2, box[2] + 4, box[3] + 2), fill=(0, 0, 0))
            draw.text((x + 6, y + 4), labels[i], fill=(255, 255, 0), font=font)
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def preview_jpeg(path: str | Path, max_width: int | None = 1280) -> tuple[bytes, tuple[int, int]]:
    """A capture scaled down for Claude (JPEG bytes, original size)."""
    import io

    from PIL import Image

    im = Image.open(path).convert("RGB")
    size = im.size
    if max_width and im.width > max_width:
        im = im.resize((max_width, round(im.height * max_width / im.width)))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return buf.getvalue(), size
