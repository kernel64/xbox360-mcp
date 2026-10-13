"""A small fake XBDM console for tests and offline development.

    python tests/mock_xbdm.py --port 7300
    XBOX360_HOST=127.0.0.1 XBOX360_PORT=7300 xbox360 info
"""

from __future__ import annotations

import socket
import socketserver
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from xbox360_mcp.framebuffer import tiled_xy  # noqa: E402
from xbox360_mcp.protocol import parse_kv  # noqa: E402

FT = 0x01DB1234_56789ABC  # some FILETIME


def make_screen() -> tuple[np.ndarray, bytes]:
    w, h, aw, ah = 1280, 720, 1280, 736
    yy, xx = np.mgrid[0:ah, 0:aw]
    img = np.zeros((ah, aw, 4), np.uint8)  # A R G B
    img[..., 0] = 255
    img[..., 1] = (xx * 255 // aw).astype(np.uint8)
    img[..., 2] = (yy * 255 // ah).astype(np.uint8)
    img[..., 3] = ((xx // 64 + yy // 64) % 2 * 255).astype(np.uint8)
    n = aw * ah
    x, y = tiled_xy(np.arange(n, dtype=np.int64), aw, 4)
    raw = img[y, x][:, [3, 2, 1, 0]].reshape(-1).tobytes()  # stored as B G R A, like a real console
    return img[:h, :w], raw


class Console:
    def __init__(self):
        self.lock = threading.Lock()
        self.files: dict[str, bytes] = {
            "hdd:\\apps\\demo\\default.xex": b"XEX2" + b"\0" * 60,
            "hdd:\\log.txt": b"line1\nline2\nline3\n",
            "hdd:\\apps\\demo\\notes, v2.txt": b"comma",
            "hdd:\\content\\0000000000000000\\profile": b"PROFILE",
        }
        self.dirs: set[str] = {
            "hdd:\\", "hdd:\\apps", "hdd:\\apps\\demo", "usb0:\\", "hdd:\\devkit",
            "hdd:\\content", "hdd:\\content\\0000000000000000",
        }
        self.mem = bytearray(0x10000)
        self.mem_base = 0x82000000
        # mflr r0 ; stw r12,-8(r1) ; stwu r1,-0x60(r1) ; nop ; blr
        code = bytes.fromhex("7c0802a6 9181fff8 9421ffa0 60000000 4e800020".replace(" ", ""))
        self.mem[0x100 : 0x100 + len(code)] = code
        # stack frame for backtrace test: sp=0x82008000 -> back chain 0x82008100, saved LR at 0x820080f8
        struct.pack_into(">I", self.mem, 0x8000, 0x82008100)
        struct.pack_into(">I", self.mem, 0x80F8, 0x82000110)
        self.notifiers: list[socket.socket] = []
        self.pending_boot: str | None = None
        self.stopped = False
        self.breakpoints: set[int] = set()
        self.screen, self.screen_raw = make_screen()
        self.jrpc = True

    def emit(self, line: str) -> None:
        for s in list(self.notifiers):
            try:
                s.sendall(line.encode() + b"\r\n")
            except OSError:
                self.notifiers.remove(s)


CONSOLE = Console()


def _norm(p: str) -> str:
    return p.lower().rstrip("\\") if not p.endswith(":\\") else p.lower()


class Handler(socketserver.BaseRequestHandler):
    def setup(self):
        self.buf = b""

    def send(self, s: str) -> None:
        self.request.sendall(s.encode("latin-1") + b"\r\n")

    def readline(self) -> str | None:
        while b"\r\n" not in self.buf:
            d = self.request.recv(65536)
            if not d:
                return None
            self.buf += d
        line, _, self.buf = self.buf.partition(b"\r\n")
        return line.decode("latin-1")

    def read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf += self.request.recv(65536)
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def multi(self, lines):
        self.send("202- multiline response follows")
        for l in lines:
            self.send(l)
        self.send(".")

    def handle(self):
        c = CONSOLE
        self.send("201- connected")
        while True:
            line = self.readline()
            if line is None:
                return
            verb, _, rest = line.partition(" ")
            verb = verb.lower()
            kv = parse_kv(rest)
            name = str(kv.get("name", ""))
            key = _norm(name) if name else ""
            if verb == "bye":
                self.send("200- bye")
                return
            elif verb == "dbgname":
                self.send("200- MockXenon")
            elif verb == "consoletype":
                self.send("200- testkit")
            elif verb == "systeminfo":
                self.multi(["HDD=Enabled", "Type=TestKit", "Platform=Trinity System=2.0.17559.0", "BaseKrnl=2.0.1888.0 Krnl=2.0.17559.0 XDK=2.0.21256.0"])
            elif verb == "xbeinfo":
                self.multi(['timestamp=0x00000000 checksum=0x00000000', 'name="\\Device\\Harddisk0\\Partition1\\Apps\\demo\\default.xex"'])
            elif verb == "drivelist":
                self.multi(['drivename="Hdd"', 'drivename="Usb0"'])
            elif verb == "drivefreespace":
                self.multi(["freetocallerhi=0x00000001 freetocallerlo=0x00000000 totalbyteshi=0x00000004 totalbyteslo=0x00000000 totalfreebyteshi=0x00000001 totalfreebyteslo=0x00000000"])
            elif verb == "dirlist":
                if key not in c.dirs:
                    self.send("402- file not found")
                    continue
                base = key.rstrip("\\") + "\\"
                out = []
                for d in sorted(c.dirs):
                    if d != key and d.startswith(base) and "\\" not in d[len(base):].rstrip("\\") and d.rstrip("\\") != key.rstrip("\\"):
                        out.append(f'name="{d[len(base):].rstrip(chr(92))}" sizehi=0x0 sizelo=0x0 createhi=0x{FT >> 32:08x} createlo=0x{FT & 0xFFFFFFFF:08x} changehi=0x{FT >> 32:08x} changelo=0x{FT & 0xFFFFFFFF:08x} directory')
                for f, data in sorted(c.files.items()):
                    if f.startswith(base) and "\\" not in f[len(base):]:
                        out.append(f'name="{f[len(base):]}" sizehi=0x0 sizelo=0x{len(data):x} createhi=0x{FT >> 32:08x} createlo=0x{FT & 0xFFFFFFFF:08x} changehi=0x{FT >> 32:08x} changelo=0x{FT & 0xFFFFFFFF:08x}')
                self.multi(out)
            elif verb == "getfileattributes":
                if key in c.files:
                    self.multi([f"sizehi=0x0 sizelo=0x{len(c.files[key]):x} createhi=0x0 createlo=0x0 changehi=0x0 changelo=0x0"])
                elif key in c.dirs:
                    self.multi(["sizehi=0x0 sizelo=0x0 createhi=0x0 createlo=0x0 changehi=0x0 changelo=0x0 directory"])
                else:
                    self.send("402- file not found")
            elif verb == "getfile":
                if key not in c.files:
                    self.send("402- file not found")
                    continue
                data = c.files[key]
                off = kv.get("offset", 0)
                size = kv.get("size", len(data) - off)
                data = data[off : off + size]
                self.send("203- binary response follows")
                self.request.sendall(struct.pack("<I", len(data)) + data)
            elif verb == "sendfile":
                n = kv["length"]
                parent = key.rpartition("\\")[0]
                if parent.endswith(":"):
                    parent += "\\"
                if parent not in c.dirs:
                    self.send("413- file cannot be created")
                    continue
                self.send("204- send binary data")
                c.files[key] = self.read_exact(n)
                self.send("200- OK")
            elif verb == "mkdir":
                if key in c.dirs:
                    self.send("410- file already exists")
                else:
                    c.dirs.add(key)
                    self.send("200- OK")
            elif verb == "delete":
                if "dir" in kv:
                    if any(x.startswith(key + "\\") for x in list(c.files) + list(c.dirs)):
                        self.send("411- directory not empty")
                        continue
                    c.dirs.discard(key)
                else:
                    c.files.pop(key, None)
                self.send("200- OK")
            elif verb == "rename":
                new = _norm(str(kv["newname"]))
                if key in c.files:
                    c.files[new] = c.files.pop(key)
                self.send("200- OK")
            elif verb == "magicboot":
                self.send("200- OK")
                c.pending_boot = str(kv.get("title", "dash"))
                for s in list(c.notifiers):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                c.notifiers.clear()
                return
            elif verb == "debugger":
                self.send("200- OK")
            elif verb == "notify":
                if "reconnectport" not in kv:  # same as a real console
                    self.send("400- missing reconnectport")
                    continue
                self.send("205- now a notification channel")
                c.notifiers.append(self.request)
                if c.pending_boot:
                    title = c.pending_boot
                    c.pending_boot = None

                    def boot():
                        time.sleep(0.3)
                        c.emit("execution started")
                        c.emit(f'modload name="default.xex" base=0x82000000 size=0x00010000')
                        c.emit("debugstr thread=0xf9000004 lf string=[demo] booting " + title)
                        time.sleep(0.2)
                        c.emit("debugstr thread=0xf9000004 lf string=[demo] init ok")

                    threading.Thread(target=boot, daemon=True).start()
                try:
                    while self.request.recv(1024):
                        pass
                except OSError:
                    pass
                return
            elif verb == "modules":
                self.multi(['name="xboxkrnl.exe" base=0x80040000 size=0x00100000 check=0x0 timestamp=0x0', 'name="default.xex" base=0x82000000 size=0x00010000 check=0x0 timestamp=0x0'])
            elif verb == "modsections":
                self.multi(['name=".text" base=0x82000000 size=0x8000 index=0 flags=0'])
            elif verb == "walkmem":
                self.multi(["base=0x82000000 size=0x00010000 protect=0x00000020"])
            elif verb == "threads":
                # real xbdm prints signed decimal: -117440508 == 0xf9000004
                self.multi(["-117440508", "-117440504"])
            elif verb == "threadinfo":
                self.multi(["suspend=0 priority=8 tlsbase=0x70000000 start=0x82000100 base=0x70010000 limit=0x70000000"])
            elif verb == "isstopped":
                t = kv.get("thread")
                if c.stopped and t == 0xF9000004:
                    self.send("200- exception code=0xc0000005 address=0x82000108 read=0x00000000")
                else:
                    self.send("408- not stopped")
            elif verb == "getcontext":
                regs = " ".join(f"Gpr{i}=0x{(0x82008000 if i == 1 else i):016x}" for i in range(32))
                self.multi(["Msr=0x00009030 Iar=0x82000108 Lr=0x82000104 Ctr=0x0 Cr=0x0 Xer=0x0", regs])
            elif verb == "getmem":
                a, n = kv["addr"], kv["length"]
                hexs = ""
                for i in range(n):
                    o = a + i - c.mem_base
                    hexs += f"{c.mem[o]:02x}" if 0 <= o < len(c.mem) else "??"
                self.multi([hexs[i : i + 256] for i in range(0, len(hexs), 256)])
            elif verb == "setmem":
                a = kv["addr"]
                data = bytes.fromhex(str(kv["data"]))
                o = a - c.mem_base
                c.mem[o : o + len(data)] = data
                self.send(f"200- set {len(data)} bytes")
            elif verb in ("stop", "go", "continue"):
                c.stopped = verb == "stop"
                self.send("200- OK")
            elif verb == "break":
                self.send("200- OK")
            elif verb == "screenshot":
                self.send("203- binary response follows")
                self.send(f"pitch=0x00001400 width=0x00000500 height=0x000002d0 format=0x18280186 offsetx=0x00000000 offsety=0x00000000, framebuffersize=0x{len(c.screen_raw):08x} sw=0x00000500 sh=0x000002d0 colorspace=0x00000000")
                self.request.sendall(c.screen_raw)
            elif verb == "consolefeatures" and c.jrpc:
                t = kv.get("type")
                if t == 13:
                    self.send("200- 17559")
                elif t == 15:
                    self.send("200- 2d")
                elif t == 16:
                    self.send("200- fffe07d1")
                else:
                    self.send("200- OK")
            elif verb == "consolefeatures":
                self.send("200- DEBUGGING")  # what xbdm answers when JRPC2 is not loaded
            else:
                self.send("407- unknown command")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int = 0) -> tuple[Server, int]:
    srv = Server(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 7300
    srv, port = serve(port)
    print(f"mock xbdm on 127.0.0.1:{port}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
