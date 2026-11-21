"""Capture-card broadcaster: one process owns the USB capture card and shares it with everyone.

A capture card can only be opened by one program. Without this, OBS blocked the agents' captures and
two agents couldn't capture at the same time. The broker opens the card once with one ffmpeg that has
two outputs:

- an MPEG-TS stream (H.264 + AAC, a keyframe every second) at http://127.0.0.1:<port>/xbox.ts, for
  OBS (Media Source), VLC, ffplay, and the agents' recordings (copied, not re-encoded);
- the card's own MJPEG frames, copied untouched into a ring buffer: /latest.jpg answers a photo at
  once, and bursts read successive frames.

One broker per PC: it holds an OS lock for its whole life, so a second one exits at once. It is started
on demand (`ensure_broker`) by the first capture, detached from the session that started it, and
stops after `idle` seconds without any client (0 = never, for `xbox360 capture serve`).

Run by hand:  python -m xbox360_mcp.broker --spec '<json>' --port 8360
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8360
TS_CHUNK = 188 * 64  # whole MPEG-TS packets, so a new client can start on any chunk
RING_SECONDS = 3.0


def state_dir() -> Path:
    base = Path(os.environ.get("XBOX360_CACHE_DIR") or Path.home() / ".xbox360-mcp") / "capture"
    base.mkdir(parents=True, exist_ok=True)
    return base


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Client side (used by CaptureCard, the CLI and tests)
# ---------------------------------------------------------------------------
def health(port: int, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(base_url(port) + "/health", timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError, urllib.error.URLError):
        return None


def latest_frame(port: int, after: int | None = None, timeout: float = 5.0, *, newest: bool = False) -> tuple[int, float, bytes]:
    """(sequence number, capture time, JPEG bytes) of the newest frame, or of the first one newer than
    `after` (waiting for it), or with newest=True the newest one if it is newer than `after`."""
    q = f"?after={after}&timeout={timeout:g}" if after is not None else f"?timeout={timeout:g}"
    if newest:
        q += "&newest=1"
    with urllib.request.urlopen(base_url(port) + "/latest.jpg" + q, timeout=timeout + 5) as r:
        return int(r.headers["X-Frame-Seq"]), float(r.headers["X-Frame-Time"]), r.read()


def stop(port: int) -> bool:
    try:
        with urllib.request.urlopen(base_url(port) + "/stop", timeout=3) as r:
            r.read()
        return True
    except (OSError, urllib.error.URLError):
        return False


def set_idle(port: int, seconds: float) -> None:
    with urllib.request.urlopen(f"{base_url(port)}/config?idle={seconds:g}", timeout=3) as r:
        r.read()


def _spawn_flags() -> list[int]:
    if os.name != "nt":
        return [0]
    detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    # Break away from the job of the session that starts it (if allowed), so the broker keeps
    # serving OBS and the other sessions when that session closes.
    return [detached | subprocess.CREATE_BREAKAWAY_FROM_JOB, detached]


def ensure_broker(spec: dict, port: int = DEFAULT_PORT, idle: float = 600, wait: float = 12.0) -> dict:
    """Health of the running broker, starting it first if needed. Raises RuntimeError with the
    broker's own error (e.g. the card is used by OBS) if it can't start."""
    h = health(port)
    if h and h.get("frames"):
        return h
    from .lock import _os_lock  # same OS-level guard as the console lock

    with _os_lock(state_dir() / f"spawn-{port}.guard", timeout=wait + 5):
        h = health(port)
        spawned_at = time.time()
        if not h:
            env = dict(os.environ)
            src = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
            log = state_dir() / f"broker-{port}.log"
            args = [sys.executable, "-m", "xbox360_mcp.broker", "--port", str(port), "--idle", f"{idle:g}",
                    "--spec", json.dumps(spec)]
            last_error: Exception | None = None
            for flags in _spawn_flags():
                try:
                    subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=open(log, "ab"), close_fds=True, creationflags=flags)
                    break
                except OSError as e:  # breakaway not allowed by the parent's job
                    last_error = e
            else:
                raise RuntimeError(f"could not start the capture broker: {last_error}")
        deadline = time.time() + wait
        while time.time() < deadline:
            h = health(port)
            if h and h.get("frames"):
                return h
            info = read_info(port)
            # only the broker started just now: an info file can be left by an earlier run
            if info.get("exited") and info.get("error") and info.get("started", 0) >= spawned_at - 1:
                raise RuntimeError(info["error"])
            time.sleep(0.2)
    info = read_info(port)
    raise RuntimeError(info.get("error") or f"the capture broker gave no picture within {wait:.0f} s")


def read_info(port: int) -> dict:
    try:
        return json.loads((state_dir() / f"broker-{port}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Broker process
# ---------------------------------------------------------------------------
class FrameRing:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.frames: collections.deque[tuple[int, float, bytes]] = collections.deque()
        self.seq = 0

    def push(self, jpeg: bytes) -> None:
        with self.cond:
            self.seq += 1
            now = time.time()
            self.frames.append((self.seq, now, jpeg))
            while self.frames and now - self.frames[0][1] > RING_SECONDS:
                self.frames.popleft()
            self.cond.notify_all()

    def get(self, after: int | None, timeout: float, newest: bool = False) -> tuple[int, float, bytes] | None:
        """after=None: the newest frame. Otherwise the first frame newer than `after` (consecutive
        frames), or with newest=True the newest one as long as it is newer than `after` (spaced bursts)."""
        deadline = time.time() + timeout
        with self.cond:
            while True:
                if self.frames:
                    if after is None or (newest and self.frames[-1][0] > after):
                        return self.frames[-1]
                    if not newest:
                        for f in self.frames:
                            if f[0] > after:
                                return f
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)


class Fanout:
    """Copies the MPEG-TS stream to every connected client; a client too slow to keep up is dropped."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clients: set[queue.Queue] = set()

    def add(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.add(q)
        return q

    def remove(self, q: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(q)

    def push(self, chunk: bytes) -> None:
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    self.clients.discard(q)
                    q.put(None)  # tell its handler to hang up

    def __len__(self) -> int:
        return len(self.clients)


def _hold_instance_lock(port: int):
    """OS lock held until the process dies: only one broker per port."""
    f = open(state_dir() / f"broker-{port}.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


class Broker:
    def __init__(self, spec: dict, port: int, idle: float):
        self.spec = spec
        self.port = port
        self.idle = idle
        self.ring = FrameRing()
        self.ts = Fanout()
        self.last_activity = time.time()
        self.streams = 0
        self.stopping = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.encoder = None
        self.error: str | None = None
        self.started = time.time()

    # -- info file -----------------------------------------------------------------
    def write_info(self, **extra) -> None:
        info = {
            "pid": os.getpid(), "port": self.port, "url": base_url(self.port) + "/xbox.ts",
            "started": self.started, "video": self.spec.get("video"), "audio": self.spec.get("audio"),
            "encoder": self.encoder, "error": self.error, **extra,
        }
        p = state_dir() / f"broker-{self.port}.json"
        p.write_text(json.dumps(info), "utf-8")

    # -- ffmpeg ----------------------------------------------------------------------
    def _encoder_args(self, which: str) -> list[str]:
        g = str(int(self.spec.get("fps", 30)))
        if which == "nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "vbr", "-cq", "21", "-b:v", "0",
                    "-g", g, "-bf", "0", "-forced-idr", "1", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-crf", "21", "-g", g,
                "-bf", "0", "-pix_fmt", "yuv420p"]

    def _start_ffmpeg(self, which: str, mjpeg_port: int) -> subprocess.Popen:
        from .capture import ffmpeg_exe, kill_with_us

        args = [ffmpeg_exe(), "-hide_banner", "-loglevel", "warning", *self.spec["input"], "-map", "0:v"]
        if self.spec.get("audio_map"):
            args += ["-map", self.spec["audio_map"], "-c:a", "aac", "-b:a", "160k"]
        # SPS/PPS repeated on every keyframe: a viewer joining mid-stream (OBS, a recording) can
        # start decoding at the next keyframe instead of waiting for headers it never gets
        args += [*self._encoder_args(which), "-bsf:v", "dump_extra=freq=keyframe",
                 "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "pipe:1"]
        # second output: the card's MJPEG frames as they are (encoded only for a non-MJPEG test source)
        args += ["-map", "0:v"] + (["-c:v", "copy"] if self.spec.get("mjpeg", True) else ["-c:v", "mjpeg", "-q:v", "3"])
        args += ["-f", "mjpeg", f"tcp://127.0.0.1:{mjpeg_port}"]
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        kill_with_us(proc)  # dies with the broker, so the card is never left open
        return proc

    def _read_ts(self, proc: subprocess.Popen) -> None:
        while True:
            chunk = proc.stdout.read(TS_CHUNK)
            if not chunk:
                return
            self.ts.push(chunk)

    def _read_mjpeg(self, server: socket.socket) -> None:
        server.settimeout(15)
        try:
            conn, _ = server.accept()
        except OSError:
            return
        buf = bytearray()
        skip = int(self.spec.get("warmup", 10))  # first frames after opening the card are corrupt
        with conn:
            while True:
                data = conn.recv(1 << 20)
                if not data:
                    return
                buf += data
                while True:
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2) if start != -1 else -1
                    if end == -1:
                        if start > 0:
                            del buf[:start]
                        break
                    frame = bytes(buf[start : end + 2])
                    del buf[: end + 2]
                    if skip > 0:
                        skip -= 1
                    else:
                        self.ring.push(frame)

    def _stderr(self, proc: subprocess.Popen, sink: list[str]) -> None:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line and not re.search(r"mjpeg|Last message repeated|EOI|SOF", line):
                sink.append(line)
                del sink[:-20]

    def run_capture(self) -> None:
        """Start ffmpeg (NVENC first, then libx264) and restart it if it dies; give up if the card
        can't be opened."""
        wanted = os.environ.get("XBOX360_CAPTURE_ENCODER", "auto").lower()
        order = ["nvenc", "x264"] if wanted == "auto" else (["nvenc"] if wanted == "nvenc" else ["x264"])
        restarts = 0
        while not self.stopping.is_set():
            ok = False
            for which in order:
                server = socket.socket()
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                errors: list[str] = []
                proc = self._start_ffmpeg(which, server.getsockname()[1])
                threading.Thread(target=self._stderr, args=(proc, errors), daemon=True).start()
                threading.Thread(target=self._read_mjpeg, args=(server,), daemon=True).start()
                reader = threading.Thread(target=self._read_ts, args=(proc,), daemon=True)
                reader.start()
                seq0 = self.ring.seq
                deadline = time.time() + 8
                while proc.poll() is None and self.ring.seq == seq0 and time.time() < deadline:
                    time.sleep(0.1)
                if self.ring.seq > seq0:
                    self.proc, self.encoder, self.error, ok = proc, which, None, True
                    self.write_info()
                    proc.wait()  # runs until the card goes away or we stop
                    server.close()
                    break
                proc.kill()
                proc.wait()
                server.close()
                text = "\n".join(errors[-6:])
                if "Could not run graph" in text or "already in use" in text:
                    self.error = ("capture card is in use by another program (OBS, or a capture started without "
                                  "the broker). Close it, or use the broker's stream in it instead.")
                    break  # no point trying another encoder
                self.error = f"ffmpeg ({which}) gave no picture: {text or 'no output'}"
            if self.stopping.is_set():
                return
            if not ok:
                self.write_info(exited=True)
                self.stopping.set()
                return
            restarts += 1
            if restarts > 5:
                self.error = "ffmpeg keeps stopping (card unplugged?)"
                self.write_info(exited=True)
                self.stopping.set()
                return
            time.sleep(1)

    def watchdog(self, httpd: ThreadingHTTPServer) -> None:
        while not self.stopping.wait(2):
            busy = self.streams > 0 or len(self.ts) > 0
            if busy:
                self.last_activity = time.time()
            if self.idle > 0 and time.time() - self.last_activity > self.idle:
                self.stopping.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        httpd.shutdown()

    def serve(self) -> int:
        lock = _hold_instance_lock(self.port)
        if lock is None:
            return 3  # another broker already runs on this port
        broker = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path not in ("/health", "/config"):  # checking on the broker doesn't keep it alive
                    broker.last_activity = time.time()
                try:
                    if url.path == "/health":
                        last = broker.ring.frames[-1][1] if broker.ring.frames else None
                        self._json({
                            "pid": os.getpid(), "frames": broker.ring.seq, "encoder": broker.encoder,
                            "video": broker.spec.get("video"), "audio": broker.spec.get("audio"),
                            "last_frame_age_s": round(time.time() - last, 2) if last else None,
                            "ts_clients": len(broker.ts), "idle_timeout_s": broker.idle,
                            "url": base_url(broker.port) + "/xbox.ts", "error": broker.error,
                        })
                    elif url.path == "/latest.jpg":
                        after = int(q["after"]) if "after" in q else None
                        f = broker.ring.get(after, float(q.get("timeout", 5)), newest=q.get("newest") == "1")
                        if f is None:
                            self.send_error(504, "no frame from the capture card")
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(f[2])))
                        self.send_header("X-Frame-Seq", str(f[0]))
                        self.send_header("X-Frame-Time", f"{f[1]:.4f}")
                        self.end_headers()
                        self.wfile.write(f[2])
                    elif url.path == "/xbox.ts":
                        self._stream_ts()
                    elif url.path == "/stream.mjpg":
                        self._stream_mjpeg()
                    elif url.path == "/config":
                        if "idle" in q:
                            broker.idle = float(q["idle"])
                        self._json({"idle_timeout_s": broker.idle})
                    elif url.path == "/stop":
                        self._json({"stopping": True})
                        broker.stopping.set()
                    elif url.path == "/":
                        page = (b"<!doctype html><title>Xbox 360</title><body style='margin:0;background:#000'>"
                                b"<img src='/stream.mjpg' style='width:100vw;height:100vh;object-fit:contain'>")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.end_headers()
                        self.wfile.write(page)
                    else:
                        self.send_error(404)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def _json(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream_ts(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()
                q = broker.ts.add()
                broker.streams += 1
                try:
                    while not broker.stopping.is_set():
                        try:
                            chunk = q.get(timeout=5)
                        except queue.Empty:
                            continue
                        if chunk is None:
                            return
                        self.wfile.write(chunk)
                finally:
                    broker.ts.remove(q)
                    broker.streams -= 1

            def _stream_mjpeg(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                broker.streams += 1
                try:
                    seq = None
                    while not broker.stopping.is_set():
                        f = broker.ring.get(seq, 5)
                        if f is None:
                            continue
                        seq = f[0]
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(f[2])).encode() + b"\r\n\r\n" + f[2] + b"\r\n")
                finally:
                    broker.streams -= 1

        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as e:
            self.error = f"port {self.port} is not free ({e}); set XBOX360_CAPTURE_PORT"
            self.write_info(exited=True)
            return 2
        httpd.daemon_threads = True
        self.write_info()
        threading.Thread(target=self.run_capture, daemon=True).start()
        threading.Thread(target=self.watchdog, args=(httpd,), daemon=True).start()
        httpd.serve_forever(poll_interval=0.5)
        self.write_info(exited=True)
        return 0 if not self.error else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xbox360_mcp.broker")
    p.add_argument("--spec", required=True, help="JSON: input (ffmpeg args), audio_map, mjpeg, fps, video, audio")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--idle", type=float, default=600, help="exit after this many seconds without a client (0 = never)")
    a = p.parse_args(argv)
    return Broker(json.loads(a.spec), a.port, a.idle).serve()


if __name__ == "__main__":
    sys.exit(main())
