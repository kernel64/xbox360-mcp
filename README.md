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
- **dev loop**: `xbox_deploy` only sends the files that changed, then can launch the xex, wait for
  a specific log line and even wait until the title exits (handy before reading its log file, which
  the running title keeps locked); `xbox_launch` starts a xex that's already on the console. The log
  they return starts when your title loads, not with the previous title's teardown;
- **logs**: everything the title sends through `OutputDebugString`/`DbgPrint`, plus exceptions,
  module loads and so on, captured continuously in the background;
- **screen**: framebuffer screenshot, returned straight as an image;
- **debug**: attach/detach the debugger on demand, crash report (stopped threads, registers, PC/LR
  resolved as `module+offset`, backtrace, disassembly), threads, breakpoints, stop/go;
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
| `XBOX360_ATTACH_DEBUGGER` | `0` | see below |
| `XBOX360_LOCK_LEASE` | `600` | how long a reservation lasts without activity, in seconds (see below) |
| `XBOX360_OWNER` | project folder | name other sessions see when you hold the console |

**Read-only mode.** With `XBOX360_READ_ONLY=1`, nothing can be written, deleted, launched or
rebooted, and the raw command only accepts known read commands. I'd start like that to try it out.

**Debugger.** The tool used to attach as xbdm's debugger all the time, and that turned out to be a bad
idea. While a debugger is attached, xbdm replays every first-chance fault forever, so an emulator that
relies on SEH for fastmem or its JIT just hangs. Aurora's Nova plugin also faults constantly and floods
xbdm, and XDK tools like `xbreboot` can hang too. So it stays detached now, and taking the console
even clears a debugger that something else left behind. When you actually want to look at a crash,
Claude calls `xbox_debugger(action="attach")`, reproduces it (the title then stops instead of
going back to the dashboard), runs `xbox_crash_report`, and detaches again. `xbox_unlock` detaches
too.

**Protected folders.** Even outside read-only mode, delete and rename refuse drive roots, `Flash:`,
`Hdd:\Content` (profiles, DLC, title updates) and `Hdd:\Cache`. If you know what you're doing, the
CLI has `rm --force`.

## Several sessions, one console

I often have two or three Claude Code windows open on different homebrews, and they all want the
Xbox at some point. Without some coordination, one of them launches its xex right in the middle of
the other one's test, and then both get confused by logs and screenshots that aren't theirs.

So the console can be reserved. Anything that changes it (deploy, launch, reboot, upload, memory
writes, breakpoints...) reserves it automatically if it's free. For a test that takes several steps,
Claude calls `xbox_lock` first with a short reason, and `xbox_unlock` when it's done. If another
session holds the console, the tool fails with a message saying who has it, why and for how long, and
Claude can either wait in line (`xbox_lock(wait_seconds=...)`) or go do something else. Reads
(listing files, memory, status...) are always allowed.

The reservation lives in a small file under `~/.xbox360-mcp/locks/`, so it works across every session
and the CLI on the same PC. Every `xbox_*` call from the holder renews it, and it goes away on its own
after `XBOX360_LOCK_LEASE` seconds of inactivity, or as soon as the holder's process is gone (when
you close that Claude Code window, for instance). If one ever gets stuck anyway,
`xbox360 unlock --force` clears it.

A few things I learned the hard way with several agents sharing one console:

- `xbox_lock` waits at most 110 s, because Claude Code moves longer calls to the background and agents
  then started their scripts before they actually had the console. Past that it fails with who holds
  it; calling it again keeps waiting.
- FTP uploads, `xbreboot` and other scripts don't renew the reservation. For a long scripted run, take
  it with `xbox_lock(reason=..., lease_seconds=1800)`.
- Scripts can check they really have the console before doing anything:
  `xbox360 lock-status --expect Harissa64 || exit 1`.
- A session and its subagents share the same server, so they share one reservation.

The CLI follows the same rules: write commands take the reservation for as long as they run, and
`--wait-lock 300` makes them wait their turn instead of failing. `xbox360 lock-status` tells you who
has the console.

## HDMI capture card

`xbox_screenshot` reads xbdm's framebuffer, which is handy but isn't quite what ends up on the TV. So I
plugged the console's HDMI into a cheap USB capture card (MS2109 chip, shows up as "USB Video"), and the
server can use it:

- `xbox_capture_photo`: one frame of the real HDMI output, saved as a full-size PNG;
- `xbox_capture_burst`: several frames a fixed time apart (or every frame the card delivers, ~30/s),
  which is what you want for flicker, a glitch that lasts two frames, or checking an animation;
- `xbox_capture_video`: a clip with sound (MP4, H.264 + AAC);
- `xbox_capture_record`: start a recording, launch your homebrew, read logs, whatever, then stop.

Claude doesn't get every frame back, just one contact sheet with timestamps on each thumbnail, which
keeps things light. All the files stay on disk (`~/xbox360-captures` by default) if you want to look
at them yourself. This doesn't touch the console at all, so it works on any title and doesn't need the
reservation.

There's nothing to install: ffmpeg comes with the `imageio-ffmpeg` package. The card is detected on its
own; webcams and virtual cameras (OBS...) are skipped.

### Watching the console while the agents capture

A capture card can only be opened by one program at a time. At first that meant OBS blocked the
agents, and two agents couldn't capture at once either. So now one small background process owns the
card and shares it: the first capture starts it, every session uses it, and it stops by itself after
10 minutes without anyone. It republishes the card as a local stream, and the agents' photos and
bursts get the card's own frames from it, untouched (and instantly, no more 1.5 s to open the card).

To watch it yourself:

```powershell
uv run xbox360 capture serve
```

starts it if needed, keeps it running until `xbox360 capture stop`, and prints the address. In OBS, add
a **Media Source**, uncheck "Local File", and put `http://127.0.0.1:8360/xbox.ts` as the input with
`mpegts` as the input format. VLC opens the same address (Media > Open Network Stream), and
`http://127.0.0.1:8360/` shows the picture in a browser. Open and close them as you like, the agents
don't notice. Just don't add the card itself ("USB Video") as a source in OBS: that grabs it for OBS
alone.

The stream is H.264 + AAC, encoded on the GPU (NVENC) when there is one, with a keyframe every second.
Recordings copy it without re-encoding, so several sessions can record at the same time. There's a
fraction of a second of delay, and a viewer that connects waits for the next keyframe (up to a second).
If the stream can't start (its port is taken, for instance), captures fall back to opening the card
directly as before.

| Variable | Default | |
|---|---|---|
| `XBOX360_CAPTURE_VIDEO` | auto | name of the capture device, if auto-detection picks the wrong one |
| `XBOX360_CAPTURE_AUDIO` | auto | its audio input, or `none` |
| `XBOX360_CAPTURE_SIZE` | `1920x1080` | |
| `XBOX360_CAPTURE_FPS` | `30` | MS2109 cards don't really do more than 30 in 1080p |
| `XBOX360_CAPTURE_DIR` | `~/xbox360-captures` | where photos, bursts and videos go |
| `XBOX360_CAPTURE_SHARED` | `1` | `0` = every capture opens the card itself (old behaviour) |
| `XBOX360_CAPTURE_PORT` | `8360` | port of the local stream |
| `XBOX360_CAPTURE_BROKER_IDLE` | `600` | the stream stops after this many seconds without a client |
| `XBOX360_CAPTURE_ENCODER` | `auto` | `nvenc` or `x264` to force one |
| `XBOX360_FFMPEG` | bundled | use another ffmpeg |

`xbox360 capture devices` shows what was found and whether the stream runs. It's Windows only for now
(DirectShow).

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
uv run xbox360 capture photo
uv run xbox360 capture burst 12 --interval-ms 100
uv run xbox360 capture video 30 run.mp4
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
