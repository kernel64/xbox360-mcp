import io
import time

import numpy as np
import pytest
from PIL import Image

import mock_xbdm
from xbox360_mcp import ConsoleBusy, ConsoleLock, analysis, deploy, jrpc
from xbox360_mcp.client import XboxConsole
from xbox360_mcp.framebuffer import to_png
from xbox360_mcp.notify import NotificationListener, parse_notification
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from xbox360_mcp.protocol import XbdmConnectionError, XbdmError, is_protected_path, is_read_only_command, normalize_path, parse_kv


@pytest.fixture(scope="module")
def port():
    srv, port = mock_xbdm.serve(0)
    yield port
    srv.shutdown()


@pytest.fixture
def xbox(port, tmp_path, monkeypatch):
    monkeypatch.setenv("XBOX360_CACHE_DIR", str(tmp_path / "cache"))
    x = XboxConsole("127.0.0.1", port, timeout=5)
    yield x
    x.close()


def test_parsers():
    assert normalize_path("hdd:/Apps//Foo/") == "hdd:\\Apps\\Foo"
    assert normalize_path("Hdd:", directory=True) == "Hdd:\\"
    assert normalize_path("/Usb0/x") == "Usb0:\\x"
    d = parse_kv('name="My Game" sizehi=0x0 sizelo=0x10 directory')
    assert d == {"name": "My Game", "sizehi": 0, "sizelo": 16, "directory": True}
    assert parse_notification("debugstr thread=0xf9000004 lf string=hello = world")[1] == "hello = world"
    # commas separate fields, except inside quotes
    assert parse_kv('name="a, b.txt" x=0x1, y=2,flag') == {"name": "a, b.txt", "x": 1, "y": 2, "flag": True}


def test_protected_and_read_only_rules():
    for p in ("Hdd:", "Flash:\\x", "Hdd:\\Content", "hdd:/content/0000000000000000/x", "Hdd:\\Cache\\a"):
        assert is_protected_path(p), p
    for p in ("Hdd:\\Apps", "Hdd:\\Contents", "Usb0:\\Content"):
        assert not is_protected_path(p), p
    assert is_read_only_command("getmem addr=0x82000000 length=4")
    assert is_read_only_command("  SYSTIME")
    for c in ("setmem addr=0 data=00", "consolefeatures ver=2 type=12", "setcontext thread=1", "magicboot"):
        assert not is_read_only_command(c), c


def test_comma_in_filename(xbox):
    names = [e["name"] for e in xbox.list_dir("Hdd:\\Apps\\demo")]
    assert "notes, v2.txt" in names
    assert xbox.download("Hdd:\\Apps\\demo\\notes, v2.txt") == b"comma"


def test_protected_delete_and_rename(xbox):
    with pytest.raises(PermissionError):
        xbox.delete("Hdd:\\Content\\0000000000000000", recursive=True)
    with pytest.raises(PermissionError):
        xbox.delete("Flash:\\launch.ini")
    with pytest.raises(PermissionError):
        xbox.rename("Hdd:\\Content\\0000000000000000\\profile", "Hdd:\\profile")
    assert xbox.exists("Hdd:\\Content\\0000000000000000\\profile")
    xbox.upload("Hdd:\\Content\\tmp.bin", b"x")
    assert xbox.delete("Hdd:\\Content\\tmp.bin", force=True) == 1


def test_upload_file_streams(xbox, tmp_path):
    src = tmp_path / "big.bin"
    payload = bytes(range(256)) * 1000  # > SEND_CHUNK, sent in several pieces
    src.write_bytes(payload)
    assert xbox.upload_file("Hdd:\\Devkit\\big.bin", src) == len(payload)
    assert xbox.download("Hdd:\\Devkit\\big.bin") == payload
    xbox.delete("Hdd:\\Devkit\\big.bin")


def test_info_and_fs(xbox):
    info = xbox.info()
    assert info["name"] == "MockXenon"
    assert info["system"]["Platform"] == "Trinity"
    assert "demo" in info["running_title"]
    assert xbox.drives() == ["Hdd", "Usb0"]
    names = [e["name"] for e in xbox.list_dir("Hdd:")]
    assert names[0] == "apps" and "log.txt" in names
    xbox.makedirs("Hdd:\\Devkit\\Test\\Sub")
    xbox.upload("Hdd:\\Devkit\\Test\\Sub\\a.bin", b"\x01\x02" * 100000)
    assert xbox.download("Hdd:/Devkit/Test/Sub/a.bin") == b"\x01\x02" * 100000
    assert xbox.download("Hdd:\\log.txt", offset=6, size=5) == b"line2"
    assert xbox.delete("Hdd:\\Devkit\\Test", recursive=True) == 3
    assert not xbox.exists("Hdd:\\Devkit\\Test")
    with pytest.raises(XbdmError) as e:
        xbox.download("Hdd:\\nope")
    assert e.value.code == 402


def test_memory_and_crash(xbox):
    data, bad = xbox.read_memory(0x82000100, 8)
    assert data.hex() == "7c0802a69181fff8" and not bad
    _, bad = xbox.read_memory(0x8200FFF0, 0x20)
    assert bad == [(0x82010000, 0x82010010)]
    data, bad = xbox.read_memory(0x81FFFFF8, 0x110)  # unmapped head, then mapped code
    assert bad == [(0x81FFFFF8, 0x82000000)]
    assert data[:8] == bytes(8) and data[0x108:0x10C].hex() == "7c0802a6"
    # zeros at the end of the mapped region: overlapping hits, none spilling into unmapped memory
    zeros = analysis.find_bytes(xbox, 0x8200FFF0, 0x20, "00 00 00 00")
    assert zeros == list(range(0x8200FFF0, 0x8200FFFD))
    xbox.write_memory(0x82000200, bytes.fromhex("deadbeef"))
    assert xbox.read_u32(0x82000200) == 0xDEADBEEF
    lines = analysis.disassemble_at(xbox, 0x82000108, 3, before=2)
    assert any("mflr" in l for l in lines) and any(l.startswith("=>") for l in lines)
    assert analysis.find_bytes(xbox, 0x82000000, 0x1000, "7c 08 ?? a6") == [0x82000100]
    mock_xbdm.CONSOLE.stopped = True
    rep = analysis.crash_report(xbox)
    mock_xbdm.CONSOLE.stopped = False
    t = rep["threads"][0]
    assert t["pc"] == "default.xex+0x108"
    assert t["backtrace"][:2] == ["default.xex+0x104", "default.xex+0x110"]
    assert "exception" in t["stop_reason"]


def test_jrpc(xbox):
    assert jrpc.kernel_version(xbox) == "17559"
    assert jrpc.title_id(xbox) == "FFFE07D1"
    assert jrpc.temperatures(xbox)["cpu"] == 45.0
    mock_xbdm.CONSOLE.jrpc = False
    try:
        assert jrpc.kernel_version(xbox) is None and not jrpc.available(xbox)
        assert jrpc.temperatures(xbox) is None
    finally:
        mock_xbdm.CONSOLE.jrpc = True


def test_threads_signed_decimal(xbox):
    assert xbox.threads() == [0xF9000004, 0xF9000008]


def test_screenshot_untile(xbox):
    header, raw = xbox.screenshot_raw()
    png, meta = to_png(header, raw, max_width=None)
    img = np.array(Image.open(io.BytesIO(png)))
    expected = mock_xbdm.CONSOLE.screen[:, :, 1:4]
    assert img.shape == (720, 1280, 3)
    assert np.array_equal(img, expected)


def test_deploy_launch_and_logs(xbox, tmp_path, port, monkeypatch):
    build = tmp_path / "Harissa64"
    (build / "data").mkdir(parents=True)
    (build / "default.xex").write_bytes(b"XEX2" + b"1" * 1000)
    (build / "data" / "rom.cfg").write_text("x=1")
    (build / "default.pdb").write_bytes(b"skip me")
    r1 = deploy.deploy(xbox, str(build), "Hdd:\\Devkit\\Harissa64")
    assert sorted(r1["uploaded"]) == ["data/rom.cfg", "default.xex"]
    r2 = deploy.deploy(xbox, str(build), "Hdd:\\Devkit\\Harissa64")
    assert r2["uploaded_count"] == 0 and r2["skipped_unchanged"] == 2
    (build / "default.xex").write_bytes(b"XEX2" + b"2" * 1000)
    assert deploy.deploy(xbox, str(build), "Hdd:\\Devkit\\Harissa64")["uploaded"] == ["default.xex"]

    # unchanged files (same size + mtime) are not re-hashed
    hashed = []
    real_sha1 = deploy._sha1
    monkeypatch.setattr(deploy, "_sha1", lambda p: hashed.append(p.name) or real_sha1(p))
    assert deploy.deploy(xbox, str(build), "Hdd:\\Devkit\\Harissa64")["uploaded_count"] == 0
    assert hashed == []

    # prune removes console files that disappeared locally
    (build / "data" / "rom.cfg").unlink()
    r = deploy.deploy(xbox, str(build), "Hdd:\\Devkit\\Harissa64", prune=True)
    assert r["deleted"] == ["data/rom.cfg"]
    assert not xbox.exists("Hdd:\\Devkit\\Harissa64\\data\\rom.cfg")
    with pytest.raises(ValueError):
        deploy.deploy(xbox, str(build / "default.xex"), "Hdd:\\Devkit\\Harissa64", prune=True)

    n = NotificationListener("127.0.0.1", port)
    n.start()
    deadline = time.time() + 3
    while not n.connected and time.time() < deadline:
        time.sleep(0.05)
    assert n.connected
    start = n.next_id - 1
    xbox.launch("Hdd:\\Devkit\\Harissa64\\default.xex")
    ev = n.wait_for(r"init ok", 10, since_id=start)
    assert ev is not None and ev.type == "debug"
    texts = [e.text for e in n.query(since_id=start)]
    assert any("reconnected" in t for t in texts)
    assert any("booting" in t for t in texts)
    n.stop()


@pytest.fixture
def server(port, monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("XBOX360_CACHE_DIR", str(tmp_path / "cache"))  # isolated lock files
    monkeypatch.setenv("XBOX360_HOST", "127.0.0.1")
    monkeypatch.setenv("XBOX360_PORT", str(port))
    srv = importlib.import_module("xbox360_mcp.server")
    monkeypatch.setattr(srv.cfg, "read_only", False)
    return srv


def test_server_read_only_mode(server, monkeypatch):
    monkeypatch.setattr(server.cfg, "read_only", True)
    assert server.xbox_raw_command("dbgname")["message"] == "MockXenon"
    for cmd in ("setmem addr=0x82000000 data=00", "consolefeatures ver=2 type=12"):
        with pytest.raises(ToolError, match="Refused: read-only mode"):
            server.xbox_raw_command(cmd)
    with pytest.raises(ToolError, match="read-only mode"):
        server.xbox_notify("hi")
    with pytest.raises(ToolError, match="read-only mode"):
        server.xbox_delete("Hdd:/log.txt")


def test_server_delete_protection(server):
    with pytest.raises(ToolError, match="protected path"):
        server.xbox_delete("Hdd:/Content/0000000000000000", recursive=True)
    with pytest.raises(ToolError, match="protected path"):
        server.xbox_delete("Hdd:/")
    assert server.xbox.exists("Hdd:/Content/0000000000000000/profile")


# ---------------------------------------------------------------------------
# Console reservation shared between sessions
# ---------------------------------------------------------------------------
@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XBOX360_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache" / "locks"


def test_lock_exclusive_and_wait(lock_dir):
    import threading

    a = ConsoleLock("10.0.0.5", owner="Harissa64")
    b = ConsoleLock("10.0.0.5", owner="OtherGame")
    assert a.status() is None
    assert a.acquire("RSP test")["held_by_me"]
    with pytest.raises(ConsoleBusy) as e:
        b.acquire("my test")
    assert "Harissa64" in str(e.value) and "RSP test" in str(e.value)
    assert b.status()["owner"] == "Harissa64" and not b.status()["held_by_me"]
    assert a.claim("auto: xbox_launch")["reason"] == "RSP test"  # keeps the explicit reason
    assert a.renew() and not b.renew()
    assert not b.release()  # can't release someone else's reservation
    threading.Timer(0.3, a.release).start()
    got = b.acquire("my test", wait_seconds=5, poll=0.05)
    assert got["owner"] == "OtherGame" and got["held_by_me"]
    assert ConsoleLock("10.0.0.6").status() is None  # one reservation per console
    assert b.release(force=False) and b.status() is None


def test_lock_expires_and_survives_dead_holder(lock_dir):
    import json
    import socket
    import subprocess
    import sys

    a = ConsoleLock("10.0.0.5", owner="A", lease_seconds=0.2)
    b = ConsoleLock("10.0.0.5", owner="B")
    a.acquire("short")
    with pytest.raises(ConsoleBusy):
        b.acquire()
    time.sleep(0.3)
    assert b.acquire("after expiry")["owner"] == "B"
    b.release()

    # holder process is gone (e.g. its Claude Code window was closed): reservation is free
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    b.path.write_text(json.dumps({
        "token": "dead", "owner": "ghost", "reason": "", "pid": proc.pid,
        "machine": socket.gethostname(), "acquired_at": time.time(), "expires_at": time.time() + 600,
    }), "utf-8")
    assert b.status() is None
    assert b.acquire("ghost gone")["owner"] == "B"


def test_server_respects_other_session(server):
    other = ConsoleLock("127.0.0.1", owner="other session")
    other.acquire("their test")
    with pytest.raises(ToolError) as e:
        server.xbox_mkdir("Hdd:/Devkit/locktest")
    assert "Console reserved by other session" in str(e.value) and "xbox_lock" in str(e.value)
    with pytest.raises(ToolError, match="reserved by"):
        server.xbox_raw_command("magicboot")
    assert server.xbox_raw_command("dbgname")["message"] == "MockXenon"  # reads still work
    assert server.xbox_lock_status()["owner"] == "other session"
    other.release()

    server.xbox_mkdir("Hdd:/Devkit/locktest")  # auto-reserves the free console
    st = server.xbox_lock_status()
    assert st["held_by_me"] and st["reason"] == "auto: xbox_mkdir"
    assert server.xbox_lock("deploy + RSP test")["reason"] == "deploy + RSP test"
    with pytest.raises(ConsoleBusy):
        other.acquire()
    assert server.xbox_unlock() == "console released"
    assert server.xbox_lock_status() == "free"
    server.xbox.delete("Hdd:/Devkit/locktest")


def test_cli_lock(port, lock_dir, capsys):
    from xbox360_mcp import cli

    other = ConsoleLock("127.0.0.1", owner="other session")
    other.acquire("their test")
    assert cli.main(["--host", "127.0.0.1", "--port", str(port), "mkdir", "Hdd:/Devkit/clilock"]) == 4
    assert "other session" in capsys.readouterr().err
    assert cli.main(["--host", "127.0.0.1", "--port", str(port), "ls", "Hdd:/"]) == 0  # reads allowed
    assert cli.main(["--host", "127.0.0.1", "lock-status"]) == 0
    assert "other session" in capsys.readouterr().out
    assert cli.main(["--host", "127.0.0.1", "unlock", "--force"]) == 0
    assert cli.main(["--host", "127.0.0.1", "--port", str(port), "mkdir", "Hdd:/Devkit/clilock"]) == 0
    assert other.status() is None  # the CLI released it when done
    cli.main(["--host", "127.0.0.1", "--port", str(port), "rm", "Hdd:/Devkit/clilock"])


# ---------------------------------------------------------------------------
# HDMI capture card (ffmpeg's lavfi test sources stand in for the DirectShow device)
# ---------------------------------------------------------------------------
from xbox360_mcp import capture  # noqa: E402

DSHOW_LIST = r"""
[dshow @ 0001] "Integrated Webcam" (video)
[dshow @ 0001]   Alternative name "@device_pnp_\\?\usb#vid_0c45&pid_6720"
[dshow @ 0001] "USB Video" (video)
[dshow @ 0001] "LSVCam" (none)
[dshow @ 0001] "OBS Virtual Camera" (video)
[dshow @ 0001] "Interface audionumérique (USB Digital Audio)" (audio)
[dshow @ 0001] "Réseau de microphones (Technologie Intel® Smart Sound)" (audio)
"""


def test_capture_device_detection():
    found = capture.parse_dshow_devices(DSHOW_LIST)
    assert found["video"] == ["Integrated Webcam", "USB Video", "OBS Virtual Camera"]
    assert capture.pick_video(found["video"]) == "USB Video"
    assert capture.pick_audio(found["audio"]) == "Interface audionumérique (USB Digital Audio)"
    old = '[dshow] DirectShow video devices\n[dshow]  "AVerMedia Live Gamer"\n[dshow] DirectShow audio devices\n[dshow]  "Mic"\n'
    assert capture.parse_dshow_devices(old) == {"video": ["AVerMedia Live Gamer"], "audio": ["Mic"]}
    with pytest.raises(capture.CaptureError, match="no capture card"):
        capture.pick_video(["Integrated Webcam"])
    with pytest.raises(capture.CaptureError, match="several"):
        capture.pick_video(["USB Video", "Elgato HD60 Capture"])
    busy = b"[dshow @ 0] Could not run graph (sometimes caused by a device already in use by other application)\n"
    with pytest.raises(capture.CaptureBusy):
        capture.check_ffmpeg_error(busy, "USB Video")


@pytest.fixture
def card(tmp_path):
    return capture.CaptureCard(
        # -re: produce frames in real time like a capture card (lavfi is otherwise much faster)
        source=["-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30"],
        audio_source=["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"],
        out_dir=tmp_path / "captures",
    )


def test_capture_photo_burst_video(card):
    shot = card.photo()
    assert Image.open(shot["path"]).size == (640, 360)
    b = card.burst(5, 0.1)
    assert len(b["frames"]) == 5 and b["times"] == [0.0, 0.1, 0.2, 0.3, 0.4]
    sheet = Image.open(io.BytesIO(capture.contact_sheet(b["frames"], ["a", "b", "c", "d", "e"], thumb_width=160)))
    assert sheet.format == "JPEG" and sheet.size[0] > 160 * 4
    clip = card.record(2)
    assert clip["audio"] and 1.5 < clip["seconds"] < 3
    frames, times = capture.sample_video(clip["path"], 4)
    assert len(frames) == 4 and times[0] == 0.0


def test_capture_background_recording(card):
    started = card.start_recording(max_seconds=30)
    assert started["audio"]
    time.sleep(2)
    assert card.status()["recording"]
    shot = card.photo()  # the card is open: comes from the recording's live frame
    assert shot["from_recording"] == started["recording"]
    with pytest.raises(capture.CaptureBusy):
        card.burst(3)
    done = card.stop_recording()
    assert done["path"] == started["recording"] and done["seconds"] > 2 and not done["hit_max_seconds"]
    assert card.status() == {"recording": False}
    with pytest.raises(capture.CaptureError):
        card.stop_recording()


def test_server_capture_tools(server, card, monkeypatch):
    monkeypatch.setattr(server, "card", card)
    img, meta = server.xbox_capture_photo(max_width=320)
    assert img.data[:2] == b"\xff\xd8" and "640x360" in meta
    sheet, meta = server.xbox_capture_burst(count=3, interval_ms=100)
    assert "'count': 3" in meta
    other = ConsoleLock("127.0.0.1", owner="other session")
    other.acquire("their test")
    _, meta = server.xbox_capture_photo()
    assert "other session" in meta  # warns that the picture may be someone else's test
    other.release()
    assert server.xbox_capture_record("start", max_seconds=20)["audio"]
    time.sleep(1.5)
    sheet, meta = server.xbox_capture_record("stop", preview_frames=3)
    assert sheet.data[:2] == b"\xff\xd8" and ".mp4" in meta


# ---------------------------------------------------------------------------
# Agents' bug reports (2026-11-21)
# ---------------------------------------------------------------------------
def _mcp_call(server, name, args):
    import asyncio

    return asyncio.run(server.mcp.call_tool(name, args))


def test_every_tool_reports_its_error_text(server):
    """mcp >= 2 replaces any exception that isn't a ToolError with a bare "Error executing tool X":
    agents never saw "Console reserved by ...", "unreachable", "protected path"..."""
    import asyncio

    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert names and all(hasattr(getattr(server, n), "__wrapped__") for n in names), "a tool skips @tool()"

    other = ConsoleLock("127.0.0.1", owner="other session")
    other.acquire("their test")
    try:
        with pytest.raises(ToolError) as e:
            _mcp_call(server, "xbox_mkdir", {"path": "Hdd:/Devkit/x"})
        assert not isinstance(e.value, UnexpectedToolError)
        assert "Console reserved by other session (their test)" in str(e.value)
    finally:
        other.release()
    with pytest.raises(ToolError) as e:
        _mcp_call(server, "xbox_read_memory", {"address": "zz"})
    assert not isinstance(e.value, UnexpectedToolError) and "ValueError" in str(e.value)
    with pytest.raises(ToolError, match="Not found: Hdd:/nope.bin"):
        _mcp_call(server, "xbox_download", {"remote_path": "Hdd:/nope.bin", "local_path": "x"})


def test_launch_fails_when_console_is_unreachable(server, monkeypatch):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    x = XboxConsole("127.0.0.1", dead_port, timeout=1)
    with pytest.raises(XbdmConnectionError):
        x.launch("Hdd:/Apps/demo/default.xex")  # used to return as if it had launched
    monkeypatch.setattr(server, "xbox", x)
    with pytest.raises(ToolError, match="Console unreachable"):
        server.xbox_launch("Hdd:/Apps/demo/default.xex")
    server.xbox_unlock()


def test_debugger_is_off_unless_asked(server):
    assert not server.notifier.attach_debugger
    mock_xbdm.CONSOLE.debugger = True  # left attached by an older server or an XDK tool
    assert server.xbox_lock("test")["debugger"] == "detached"
    assert mock_xbdm.CONSOLE.debugger is False
    assert server.xbox_debugger("attach") == {"attached": True}
    assert mock_xbdm.CONSOLE.debugger is True
    assert server.xbox_unlock() == "console released, debugger detached"
    assert mock_xbdm.CONSOLE.debugger is False
    assert server.xbox_debugger("status")["attached"] is False


BS = chr(92)  # backslash, for the mock console's internal keys


def test_download_streams_to_disk(xbox, tmp_path):
    big = bytes(range(256)) * 12000  # ~3 MB
    xbox.upload("Hdd:/Devkit/big.bin", big)
    out = tmp_path / "big.bin"
    assert xbox.download_to("Hdd:/Devkit/big.bin", out, chunk=4096) == len(big)
    assert out.read_bytes() == big and not (tmp_path / "big.bin.part").exists()
    with pytest.raises(XbdmError):
        xbox.download_to("Hdd:/Devkit/nope.bin", tmp_path / "nope.bin")
    assert not (tmp_path / "nope.bin").exists() and not (tmp_path / "nope.bin.part").exists()
    xbox.delete("Hdd:/Devkit/big.bin")


def test_file_held_open_by_the_title(xbox, server, tmp_path):
    key = "hdd:" + BS + "log.txt"
    mock_xbdm.CONSOLE.open_by_title.add(key)
    try:
        with pytest.raises(XbdmError, match="running title has this file open"):
            xbox.download("Hdd:/log.txt")
        with pytest.raises(ToolError, match="running title has this file open"):
            server.xbox_read_text_file("Hdd:/log.txt")
        # a folder download copies the rest and reports the file that failed
        res = server.xbox_download("Hdd:/", str(tmp_path / "hdd"))
        assert any(f.startswith("log.txt: ") for f in res["failed"]) and res["downloaded"] > 0
    finally:
        mock_xbdm.CONSOLE.open_by_title.discard(key)


def test_upload_retried_after_a_dropped_connection(xbox, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 200000)
    mock_xbdm.CONSOLE.drop_next_sendfile = True
    assert xbox.upload_file("Hdd:/Devkit/a.bin", src) == 200000
    assert xbox.download("Hdd:/Devkit/a.bin") == b"x" * 200000
    xbox.delete("Hdd:/Devkit/a.bin")


def test_outputs_stay_small_for_claude(server):
    res = server.xbox_list_dir("Hdd:/", contains="TXT")
    assert len(res["entries"]) == 1 and res["entries"][0].endswith("  log.txt")
    res = server.xbox_list_dir("Hdd:/", recursive=True, max_entries=2)
    assert len(res["entries"]) == 2 and "truncated" in res
    assert server.xbox_read_text_file("Hdd:/log.txt", grep="line[23]").endswith("line2" + chr(10) + "line3")
    lines = server._fit_lines([f"line {i} " + "x" * 90 for i in range(2000)])
    assert sum(len(l) for l in lines) < server.MAX_TEXT + 200 and lines[0].startswith("[... ") and lines[-1].startswith("line 1999")
    assert server._fit("a" * 100 + chr(10) + "b" * 100, 150).endswith("more characters cut]")


def test_ffmpeg_dies_with_the_server(tmp_path):
    """An orphaned ffmpeg kept the capture card blocked after Claude Code crashed."""
    import subprocess
    import sys

    child = tmp_path / "child.py"
    child.write_text(
        "import sys, time\n"
        "from xbox360_mcp.capture import CaptureCard\n"
        "c = CaptureCard(source=['-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30'], out_dir=sys.argv[1])\n"
        "c.start_recording(max_seconds=120, audio=False)\n"
        "print(c.recording.proc.pid, flush=True)\n"
        "time.sleep(120)\n",
        "utf-8",
    )
    proc = subprocess.Popen([sys.executable, str(child), str(tmp_path / "cap")], stdout=subprocess.PIPE, text=True)
    ffmpeg_pid = int(proc.stdout.readline())
    assert capture_pid_alive(ffmpeg_pid)
    proc.kill()  # like a crash: no atexit, no clean stop
    proc.wait()
    deadline = time.time() + 5
    while capture_pid_alive(ffmpeg_pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not capture_pid_alive(ffmpeg_pid)


def capture_pid_alive(pid):
    from xbox360_mcp.lock import pid_alive

    return pid_alive(pid)


def test_title_matches():
    from xbox360_mcp.protocol import title_matches

    running = BS.join(["", "Device", "Harddisk0", "Partition1", "Emulators", "HarissaDS", "default.xex"])
    assert title_matches(running, "Hdd:/Emulators/HarissaDS/default.xex")
    assert not title_matches(running, "Hdd:/Emulators/HarissaPS2/default.xex")
    assert not title_matches(None, "Hdd:/x/default.xex")


def test_launch_log_starts_at_the_new_title(server):
    res = server.xbox_launch("Hdd:/Apps/demo/default.xex", wait_for="init ok", timeout_seconds=10)
    assert res["wait_for"]["matched"] and res["skipped_teardown_lines"] >= 30
    assert not any("terminate thread" in l for l in res["log"]) and "modload" in res["log"][0]
    # Aurora's own modunload, printed before the title loads, must not satisfy wait_for
    res = server.xbox_launch("Hdd:/Apps/demo/default.xex", wait_for="modunload", timeout_seconds=3)
    assert res["wait_for"]["timed_out"]
    server.xbox_unlock()


def test_launch_waits_for_the_title_to_exit(server):
    import threading

    aurora = BS.join(["", "Device", "Harddisk0", "Partition1", "Apps", "Aurora", "Aurora.xex"])

    def title_quits():
        time.sleep(3)
        mock_xbdm.CONSOLE.running = aurora

    threading.Thread(target=title_quits, daemon=True).start()
    res = server.xbox_launch("Hdd:/Apps/demo/default.xex", wait_exit_seconds=20)
    assert res["exit"]["exited"] and res["exit"]["seen_running"] and "Aurora" in res["exit"]["now_running"]
    res = server.xbox_launch("Hdd:/Apps/demo/default.xex", wait_exit_seconds=3)
    assert res["exit"]["exited"] is False  # still running when the wait ends
    server.xbox_unlock()


def test_rename_overwrite(xbox):
    xbox.upload("Hdd:/Devkit/new.xex", b"new")
    xbox.upload("Hdd:/Devkit/old.bak", b"old")
    with pytest.raises(XbdmError, match="already exists"):
        xbox.rename("Hdd:/Devkit/new.xex", "Hdd:/Devkit/old.bak")
    assert xbox.rename("Hdd:/Devkit/new.xex", "Hdd:/Devkit/old.bak", overwrite=True) is True
    assert xbox.download("Hdd:/Devkit/old.bak") == b"new" and not xbox.exists("Hdd:/Devkit/new.xex")
    xbox.upload("Hdd:/Devkit/x.bin", b"x")
    with pytest.raises(XbdmError, match="is a folder"):
        xbox.rename("Hdd:/Devkit/x.bin", "Hdd:/Apps", overwrite=True)
    for f in ("old.bak", "x.bin"):
        xbox.delete("Hdd:/Devkit/" + f)


def test_lock_lease_wait_and_renewal(server):
    import json

    info = server.xbox_lock("long scripted run", lease_seconds=1800)
    assert info["held_by_me"] and info["expires_in_s"] > 1700
    # every xbox_* call renews the lease, lock_status included
    path = server.console_lock.path
    d = json.loads(path.read_text("utf-8"))
    d["expires_at"] = time.time() + 5
    path.write_text(json.dumps(d), "utf-8")
    assert server.xbox_lock_status()["expires_in_s"] > 1700
    server.xbox_unlock()
    assert server.console_lock.lease_seconds == server.cfg.lock_lease

    other = ConsoleLock("127.0.0.1", owner="other session")
    other.acquire("their test")
    t = time.time()
    with pytest.raises(ToolError) as e:
        server.xbox_lock("mine", wait_seconds=1)
    assert time.time() - t < 5
    assert "Still busy after waiting 1 s" in str(e.value) and "other session (their test)" in str(e.value)
    assert server.MAX_LOCK_WAIT < 120
    other.release()


def test_cli_lock_status_expect(port, lock_dir, capsys):
    from xbox360_mcp import cli

    assert cli.main(["--host", "127.0.0.1", "lock-status", "--expect", "Harissa64"]) == 5
    other = ConsoleLock("127.0.0.1", owner="Harissa64")
    other.acquire("regression run")
    assert cli.main(["--host", "127.0.0.1", "lock-status", "--expect", "harissa64"]) == 0
    assert cli.main(["--host", "127.0.0.1", "lock-status", "--expect", "CubeX"]) == 5
    other.release()


def test_screenshot_says_which_title_runs(server):
    _, meta = server.xbox_screenshot(max_width=320)
    assert "running_title" in meta
