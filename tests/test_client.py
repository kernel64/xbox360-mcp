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
from xbox360_mcp.protocol import XbdmError, is_protected_path, is_read_only_command, normalize_path, parse_kv


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
        with pytest.raises(PermissionError):
            server.xbox_raw_command(cmd)
    with pytest.raises(PermissionError):
        server.xbox_notify("hi")
    with pytest.raises(PermissionError):
        server.xbox_delete("Hdd:/log.txt")


def test_server_delete_protection(server):
    with pytest.raises(PermissionError):
        server.xbox_delete("Hdd:/Content/0000000000000000", recursive=True)
    with pytest.raises(PermissionError):
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
    with pytest.raises(ConsoleBusy) as e:
        server.xbox_mkdir("Hdd:/Devkit/locktest")
    assert "other session" in str(e.value) and "xbox_lock" in str(e.value)
    with pytest.raises(ConsoleBusy):
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
