# xbox360-mcp

I got tired of bouncing between my PC and the Xbox every time I changed something in a homebrew:
copy the xex over FTP, launch it from Aurora, stare at the TV, guess why it crashed...
So I wrote this small tool that talks directly to xbdm (the 360 debug monitor, port 730).
You can use it three ways:

- as an MCP server, so Claude Code can deploy, launch, read the logs, take screenshots and dig
  into crashes on its own;
- from the command line (`xbox360 ...`), for build scripts;
- as a small Python library.

It works on an RGH/JTAG console with xbdm loaded. I use it on a Jtag Trinity
(kernel 17559, Aurora 0.7b).

## Console side

`xbdm.xex` has to be loaded by DashLaunch. In `launch.ini`:

```ini
[Plugins]
plugin1 = Hdd:\xbdm.xex
plugin2 = Hdd:\JRPC2.xex   ; optional
```

JRPC2 isn't required, it's only used for temperatures, the title ID and on-screen notifications
(XNotify). Everything else works without it.

The console and the PC need to be on the same network. To check that xbdm is answering:

```powershell
Test-NetConnection 192.168.1.50 -Port 730
```

(use your console's IP, Aurora shows it at the bottom of the screen). Every connection is opened
from the PC, so there's nothing to open in the firewall.

## Install

I use [uv](https://docs.astral.sh/uv/):

```powershell
git clone https://github.com/kernel64/xbox360-mcp
cd xbox360-mcp
uv sync
$env:XBOX360_HOST = "192.168.1.50"
uv run xbox360 info
```

If `info` prints the console name and the running title, you're good.
Without uv, `pip install -e .` in a venv does the job.

## With Claude Code

```powershell
claude mcp add xbox360 --scope user -e XBOX360_HOST=192.168.1.50 -- uv --directory C:\path\to\xbox360-mcp run xbox360-mcp
```

You can also declare it per project with a `.mcp.json` (there's a template in
`examples/mcp.example.json`). After that, `/mcp` in Claude Code should show `xbox360` as connected.

`examples/CLAUDE-snippet.md` is a piece of text to paste into your homebrew project's `CLAUDE.md`:
it tells Claude which folder to deploy and which log line to wait for at startup. With that, you can
ask things like "build in Release, deploy, launch, wait for `init ok` and if it crashes show me the
crash report", and it figures it out.

What it has to work with:

- **console**: status, drives, reboot, on-screen notification, raw xbdm command;
- **files**: list, download, read a text file, upload, create, rename, delete;
- **dev loop**: `xbox_deploy` only sends the files that changed, then can launch the xex and wait for
  a specific log line; `xbox_launch` starts a xex that's already on the console;
- **logs**: everything the title sends through `OutputDebugString`/`DbgPrint`, plus exceptions,
  module loads and so on, captured continuously in the background;
- **screen**: framebuffer screenshot, returned straight as an image;
- **debug**: crash report (stopped threads, registers, PC/LR resolved as `module+offset`, backtrace,
  disassembly), threads, breakpoints, stop/go;
- **memory**: read, write, pattern search with wildcards, PowerPC disassembly (capstone),
  memory map, modules.

## Configuration

Everything goes through environment variables:

| Variable | Default | |
|---|---|---|
| `XBOX360_HOST` | | console IP (required) |
| `XBOX360_PORT` | `730` | |
| `XBOX360_TIMEOUT` | `10` | in seconds |
| `XBOX360_DEPLOY_ROOT` | `Hdd:\Devkit` | where `xbox_deploy` puts builds by default |
| `XBOX360_LOG_FILE` | | if set, every log line is also written to this file |
| `XBOX360_READ_ONLY` | `0` | see below |
| `XBOX360_ATTACH_DEBUGGER` | `1` | see below |

**Read-only mode.** With `XBOX360_READ_ONLY=1`, nothing can be written, deleted, launched or
rebooted, and the raw command only accepts known read commands. I'd start like that to try it out.

**Debugger.** By default the tool attaches as a debugger. The point is that a crash freezes the game
instead of dropping back to the dashboard, so you can actually go look at what happened. In read-only
mode it doesn't attach, unless you force `XBOX360_ATTACH_DEBUGGER=1`.

**Protected folders.** Even outside read-only mode, delete and rename refuse drive roots, `Flash:`,
`Hdd:\Content` (profiles, DLC, title updates) and `Hdd:\Cache`. If you know what you're doing, the
CLI has `rm --force`.

## Command line

```powershell
uv run xbox360 info
uv run xbox360 ls Hdd:\Apps
uv run xbox360 deploy .\build\Release Hdd:\Devkit\MyGame --run --wait "init ok"
uv run xbox360 log -f
uv run xbox360 screenshot capture.png
uv run xbox360 mem 82000000 100 --format u32
uv run xbox360 dis 82001234 20
uv run xbox360 crash
uv run xbox360 raw dmversion
```

For `mem`, both the address and the length are hex (`100` = 256 bytes). `deploy --prune` also
deletes files on the console that you removed locally. Careful: that includes anything the game
wrote into that folder itself (saves, configs).

## Python

```python
from xbox360_mcp import XboxConsole

x = XboxConsole("192.168.1.50")
print(x.info())
x.upload_file("Hdd:\\Devkit\\test.bin", "test.bin")
data, unreadable = x.read_memory(0x82000000, 0x100)
```

## Tests

`tests/mock_xbdm.py` is a fake console that mimics xbdm (files, memory, threads, logs, screenshot,
JRPC2). The tests run against it:

```powershell
uv run --extra dev pytest
```

You can also run it on its own (`python tests/mock_xbdm.py --port 7300`) and point the tool at it
with `XBOX360_HOST=127.0.0.1` and `XBOX360_PORT=7300` to play around without an Xbox.

## A few notes

- Screenshots are BGRA on a real console. If colors ever look off, `channel_order` (or `--order` on
  the CLI) lets you try another byte order.
- Without JRPC2, xbdm answers `DEBUGGING` to every `consolefeatures` command. The tool detects that
  and just treats the plugin as missing.
- xbdm only takes about 4 connections at once. If you get a `401` error, close the other tools
  (Neighborhood, etc.).
- No memory breakpoints or dedicated single-stepping yet. For now that goes through the raw command
  (`break read=0x... size=4`, `step`...).
- Some replies (`getcontext` in particular) can vary between xbdm versions. If something's missing,
  the raw command shows the reply as-is.
