## Xbox 360 (`xbox360` MCP server)

The RGH console is reachable through the `xbox_*` tools.

- Build output: `build\Release\` (contains `default.xex`)   <!-- adjust -->
- Console folder: `Hdd:\Devkit\MyGame`                       <!-- adjust -->
- Log line printed when init is OK: `init ok`                <!-- adjust: regex -->

Test loop after each change:
1. Build.
2. `xbox_deploy(local_dir=<output>, remote_dir=<console folder>, launch=true, wait_for=<init log>)`.
3. Read what follows with `xbox_debug_log(since_id=<previous next_since_id>)`.
4. On `timed_out` or an `[exception]` line: `xbox_crash_report`, then match PC/LR/backtrace against the
   source (module `default.xex`, usually based at 0x82000000; use the build's .map/.pdb to find the function).
5. `xbox_screenshot` to check the rendering.
6. After a crash: `xbox_reboot` before launching again.

Log with `OutputDebugStringA` / `DbgPrint` in the homebrew: that's what `xbox_debug_log` picks up.
