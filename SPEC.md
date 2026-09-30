# Claude Touch Bar (CTB): Requirements Spec

| | |
|---|---|
| Status | v0.4 · 2026-09-30 · implemented in Python, see **§12 Implementation status** |
| Target hardware | **MacBookPro13,3** (2016 15", A1707), **Apple T1 / iBridge `05ac:8600`** |
| Target OS | Ubuntu 22.04, kernel 6.8.0 (stock), GNOME 42 on Wayland |
| Scope | Claude Code only; multiple concurrent sessions |
| Inspiration | [claude-pulse](https://github.com/NoobyGains/claude-pulse) (status and usage metrics), [Vibe Island](https://vibeisland.app/) (answering agent prompts in place) |

Requirement IDs (`R-xx`) map to acceptance tests in §10. **[M0]** marks behavior that must be confirmed on this machine before it is built.

---

## 0. Hardware background

### 0.1 The current situation
This Mac currently runs `apple-ib-tb` + `apple-ibridge`. With these drivers the **T1 firmware draws the Touch Bar itself**, and Linux can only switch between Apple's built-in layouts (`fnmode`, `idle_timeout`, `dim_timeout`). **No custom content is possible in this mode.**

### 0.2 How the Touch Bar becomes a real display
The iBridge has a second USB configuration (**config 2**) with a display interface ("DFR") that takes host-rendered frames, plus a digitizer interface for touch. Two community drivers use it:

| | **A. barkeep** (primary) | **B. appletbdrm, sunplex07 fork** (alternative) |
|---|---|---|
| What it exposes | `/dev/dfr0`: write one raw RGB24 frame; the driver keeps resending it | DRM device `/dev/dri/cardN`, so tiny-dfr works unchanged |
| Resolution | 2170 × 60 | same panel (read from DRM mode) |
| Touch | Userspace reads the digitizer: float32 X in [0.5, 1.0]; release is inferred from a gap of about 120 ms | Needs t2linux `hid-appleib`/`hid-multitouch` **kernel patches**, so a custom kernel |
| Stock Ubuntu kernel | Yes (DKMS) | No, touch does not work without a patched kernel |
| Tested models | MacBookPro13,2 (13"); **13,3 not yet confirmed** | lists A1706 and **A1707** |
| Known issues | **Suspend hangs the machine** | Touch patches must be rebased for each kernel |
| Conflicts | Must unload `apple_ib_tb`, `apple_ib_als`, `apple_ibridge`, which force config 1 | same |

**Decision:** CTB ships its own renderer, `ctb-bar`, behind a `DisplayBackend` trait. **Backend A (barkeep `/dev/dfr0`) is the primary target**, because it works on the stock kernel. Backend B (DRM) is the alternative if A fails on 13,3.

### 0.3 Consequences
Once the host drives the Touch Bar, **the firmware strip is gone**. CTB must therefore redraw the controls the firmware used to provide: Esc, F1–F12, brightness, volume, media and keyboard backlight. The Linux ALS driver (`apple_ib_als`) is unloaded too, but the T1 still auto-adjusts the bar backlight itself (R-34).

- **R-01** On this hardware, `ctb-bar` is the only process that owns the Touch Bar display and digitizer.
- **R-02** `ctb-bar` provides a complete function-row layer (Esc, F1–F12, brightness, volume, mute, play/pause, keyboard backlight). The keys go through a `uinput` virtual keyboard.
- **R-03** If `ctb-bar` exits, the stock drivers are restored by `ctb-display down` (systemd `ExecStopPost`): barkeep is unloaded, the iBridge is re-enumerated into USB config 1, `modprobe -a apple_ibridge apple_ib_tb apple_ib_als` runs, and both iBridge HID interfaces are rebound to `apple-ibridge-hid`, so the stock driver works again (keys, touch, brightness) within about 10 s (about 20 s if it needs its one retry). **Verified limitation (MacBookPro13,3, 2026-09-30):** after a display session the T1 firmware does not draw the function-row icons again until reboot; re-enumeration, a USB port reset and resending display-on/mode do not bring them back. So ctb-bar must own the whole row (fkeys/media layers) while it runs, and a crash leaves an invisible-but-working row until it restarts.
- **R-04** On suspend, a systemd sleep hook stops `ctb-bar` and restores the stock drivers **before** suspending, and reverses this on resume. This avoids barkeep's known hang **[M0]**.

---

## 1. Goals / non-goals

### Goals
- G1: The Touch Bar shows at a glance what Claude is doing: activity state, context and usage limits, cost, model/effort/branch.
- G2: Answer permission prompts, plan approval and questions by touching the Touch Bar.
- G3: A normal function row stays available at all times (Fn and Esc work as before).
- G4: Fail safe. If CTB is broken, Claude Code prompts in the terminal as usual, and the Touch Bar falls back to the firmware strip.
- G5: Everything stays on the machine: no network, no telemetry.

### Non-goals (v1)
- Other agents
- macOS
- Remote or cloud sessions
- Sounds
- Jumping to the terminal tab
- Showing the full plan text on the bar
- An on-screen GUI (a desktop notification is the only fallback, §4.7)

---

## 2. Architecture

```
┌─────────────┐ hooks (JSON on stdin)  ┌──────────┐
│ Claude Code │───────────────────────►│ ctb-hook │──┐
│ (N sessions)│ statusLine (JSON)      ┌────────────┐ │ $XDG_RUNTIME_DIR/ctb.sock (0600)
│             │───────────────────────►│ ctb-status │─┤ newline-delimited JSON
└─────────────┘◄── decision JSON ──────└────────────┘ ▼
                                 ┌─────────────────────────────────┐
                                 │ ctb-bridge (systemd --user)     │
                                 │ sessions, state machine, prompt │
                                 │ queue, layout model             │
                                 └───────────────┬─────────────────┘
                                                 │ /run/ctb/render.sock (0660, group ctb)
                                                 ▼ layout ↓  taps ↑
                                 ┌─────────────────────────────────┐
                                 │ ctb-bar (system service, root)  │
                                 │ cairo/pango renderer, touch,    │
                                 │ function row, uinput, dimming   │
                                 │ DisplayBackend: dfr0 | drm      │
                                 └───────────────┬─────────────────┘
                                                 ▼
                                     T1 iBridge (USB config 2)
```

| Component | Language | Responsibility |
|---|---|---|
| `ctb-hook` | Rust, static binary | Registered for the hook events. Forwards the event. For blocking events, waits for a decision and prints the hook output JSON. |
| `ctb-status` | Rust | The `statusLine` command. Tees the JSON to the bridge. Prints the terminal line, either by passing the same stdin through to an existing status line (e.g. claude-pulse) or by printing a minimal built-in line. |
| `ctb-bridge` | Rust (tokio) | The single source of truth for sessions, states, metrics and pending prompts. Decides what to show, sends a **layout model** (segments + buttons, not pixels), and resolves prompts from the taps it receives. |
| `ctb-bar` | Rust (cairo-rs, pango, evdev, uinput) | Owns the display and touch. Draws the function row plus the Claude region, hit-tests touches, handles Fn and dimming, and falls back to the function row alone while the bridge is disconnected. |

- **R-05** The bridge sends *what* to show. `ctb-bar` decides *how* to draw it: fonts, truncation, animation.
- **R-06** If the bridge disconnects, `ctb-bar` shows the function row only and draws the Claude region as `Claude: offline` (dim). It reconnects every 2 s.

### 2.1 Display backend interface
```rust
trait DisplayBackend {
    fn size(&self) -> (u32, u32);                   // 2170×60 expected [M0 verify on 13,3]
    fn present(&mut self, rgb24: &[u8]) -> Result<()>;
    fn set_brightness(&mut self, level: u8) -> Result<()>;   // 0 = off
}
trait TouchSource {
    fn next(&mut self) -> TouchEvent;                // Down{x_px} | Move{x_px} | Up
}
```
- **dfr0 backend.** `present` writes one frame of 390,600 bytes to `/dev/dfr0`. The touch source reads the digitizer (interface 2, EP 0x83), maps X from [0.5, 1.0] to pixels (calibrated in M0), and synthesizes `Up` after 120 ms without a report. It does **not** use barkeep's `dfr-bar.py` UI, only its kernel modules.
- **drm backend.** Dumb buffer plus page flip, touch from evdev. This backend only matters if B is chosen.
- **R-07** Frames are sent only when something changes, at most **30 fps**. The spinner animation runs at 10 fps.

---

## 3. Data sources and state model

### 3.1 Hook events used
All hook inputs include `session_id`, `cwd`, `transcript_path`, `permission_mode`, `effort.level`, `hook_event_name`, and `agent_id` when the hook runs inside a subagent.

| Event | Matcher | Blocking | Effect |
|---|---|---|---|
| `SessionStart` / `SessionEnd` | – | no | Register / remove the session. Label = `basename(cwd)`, with a suffix on collision. |
| `UserPromptSubmit` | – | no | **Thinking**: start the timer and clear stale prompts. |
| `PreToolUse` | `*` | no* | **Tool**: `<Tool>: <summary>` (§3.3). |
| `PostToolUse` / `PostToolUseFailure` | `*` | no | Back to **Thinking**. The failure event flashes red. Clears the prompt for that `tool_use_id`. |
| `PermissionRequest` | `*` | **yes** | **Permission** prompt, or **Plan** prompt when `tool_name == ExitPlanMode`. |
| `SubagentStart` / `SubagentStop` | – | no | Subagent counter. |
| `PreCompact` / `PostCompact` | – | no | **Compacting** flag. |
| `Notification` | `permission_prompt`, `idle_prompt`, `agent_needs_input` | no | Fallback signal that input is needed (R-24). `idle_prompt` → **Idle**. |
| `Stop` | – | no | **Done** (3 s) → **Idle**. Clears prompts. |
| `StopFailure` | – | no | **Error** until the next prompt is submitted. |

\* `PreToolUse` with matcher `AskUserQuestion` may become blocking, depending on the §6 spike.

### 3.2 statusLine JSON → metrics (as in claude-pulse)
| Field | Shown as |
|---|---|
| context-window used % | `ctx ▮▮▮▯ 62%` |
| 5-hour limit % | `5h ▮▮▯ 41%` |
| 7-day limit % | `7d 18%` |
| session cost | `$1.24` |
| model · effort · git branch | `Opus·high·main` (branch read from `cwd`, cached for 5 s) |

- **R-10** If a field is missing, it is hidden. It is never shown as `0`.
- **R-11** Bars are green below 70%, yellow from 70% to below 90%, red at 90% or above.
- **R-12** If metrics are older than 60 s, they are drawn dimmed.

### 3.3 Tool summaries
| Tool | Summary |
|---|---|
| Bash | first line of `command`, truncated |
| Read / Edit / Write | file basename |
| Grep / Glob | pattern |
| WebFetch / WebSearch | host / query |
| Task / Agent | subagent type |
| MCP `mcp__srv__tool` | `srv:tool` |
| other | tool name |

### 3.4 Session state machine
```
            UserPromptSubmit              PreToolUse
  Idle ─────────────────────► Thinking ◄──────────────► Tool
   ▲                            │  ▲  PostToolUse         │ PermissionRequest
   │ 3 s                        │  │                      ▼
  Done ◄──────── Stop ──────────┘  └── decision ── NeedsInput{Permission|Plan|Question}
                                                          │ timeout
                                                          ▼
                                                 NeedsInput{Terminal}
  any ── StopFailure ──► Error        any ── PreCompact ──► Compacting (flag)
```

---

## 4. Touch Bar UI (2170 × 60 px)

The width is shared between a fixed **left** part (Esc), the **Claude region** in the middle (flex width), and a **right control strip** (brightness, volume, mute, play/pause). The right strip can be collapsed.

### 4.1 Status mode
```
┌─────┬────────┬──────────────────────────────┬────────────┬───────────┬───────┬───────┬───────────────┬──────┬──────┬──────┐
│ esc │ ARX +1 │ ⚙ Bash: cargo test  1:42  ⑂2 │ ctx ▮▮▮▯62%│ 5h ▮▮▯ 41%│ 7d 18%│ $1.24 │ Opus·high·main│ ☀︎   │ 🔊   │ ⏯    │
└─────┴────────┴──────────────────────────────┴────────────┴───────────┴───────┴───────┴───────────────┴──────┴──────┴──────┘
  fixed  chip     activity (flex)                metrics (dropped right-to-left when space is short)     control strip
```
- **R-13** State colors: Idle grey, Thinking/Tool blue with a spinner, NeedsInput amber and pulsing, Done green, Error red.
- **R-14** The turn timer shows `m:ss` since `UserPromptSubmit`. Subagents appear as an overlay `⑂N`.
- **R-15** When space runs out, segments are dropped in this order: model/branch, cost, 7d, 5h. The chip, activity and ctx are never dropped.
- **R-16** Tapping the **session chip** cycles the displayed session. Tapping **activity** shows the metrics row for that session for 5 s.
- **R-17** With no Claude sessions at all, the Claude region shows the normal special keys, like the firmware default: brightness, keyboard backlight, media, volume.

### 4.2 Function-row layer
- **R-18** Holding **Fn** shows `esc F1 … F12` across the whole bar, even while a prompt is pending. Releasing Fn restores the previous view. **[M0]** Confirm that the Fn key is visible to `ctb-bar` through the `applespi` keyboard evdev (`KEY_FN`).
- **R-19** Config option `default_layer = "claude" | "fkeys" | "media"` sets what the bar shows when Fn is not held. The default is `claude`.

### 4.3 Permission mode
```
┌─────┬─────────┬──────────────────────────────────────────────────┬──────────┬────────────┬──────────┐
│ esc │ ARX ⚠   │ Bash: rm -rf build/ && cargo build --release …   │   Deny   │  Allow ▓▓░ │  Always  │
└─────┴─────────┴──────────────────────────────────────────────────┴──────────┴────────────┴──────────┘
```
- **R-20** The Claude region takes the full width minus Esc; the control strip is hidden. It shows the session, the tool, and the summary (with a longer limit than in status mode). Swiping left or right on the summary scrolls long commands.
- **R-21** **Allow** fires only after a **hold of 400 ms** (configurable), shown as a filling bar. Lifting early cancels it. **Deny** fires on a single tap.
- **R-22** **Always** (off by default) returns `allow` with `updatedPermissions` (scope `session`, rule such as `Bash(cargo build *)`).
- **R-23** Dangerous commands are shown in red. For them, Allow needs a 1000 ms hold and **Always** is hidden. The default patterns are `rm -rf`, `sudo`, `git push --force|-f`, `git reset --hard`, `curl … | sh`, `dd `, `mkfs`, `chmod -R 777`, and writes outside `cwd`. The list is configurable.
- **R-24** If a `Notification: permission_prompt` arrives with no matching pending request, show amber **"Waiting in terminal"** with no buttons.

### 4.4 Plan mode (`PermissionRequest` for `ExitPlanMode`)
```
┌─────┬─────────┬─────────────────────────────────────────────────────┬──────────┬─────────────┐
│ esc │ ARX 📋  │ Plan ready: Write SPEC.md for Touch Bar …           │  Reject  │ Approve ▓▓░ │
└─────┴─────────┴─────────────────────────────────────────────────────┴──────────┴─────────────┘
```
- **R-25** The title comes from the first Markdown heading of the plan. Swiping on the title scrolls through the plan's headings.
- **R-26** **Approve** (a 400 ms hold) returns `allow`. **Reject** returns `deny` with "Plan rejected from Touch Bar — please revise." Detailed feedback is given in the terminal.

### 4.5 Question mode (AskUserQuestion)
```
┌─────┬─────────┬───────────────────────────┬───────────┬───────────┬───────────┬───────────┬──────────┐
│ esc │ ARX ?   │ Which OS drives the bar?  │  macOS    │ T2 Linux  │  Both     │  Other…   │ Terminal │
└─────┴─────────┴───────────────────────────┴───────────┴───────────┴───────────┴───────────┴──────────┘
```
- **R-27** Shows the question and up to 4 options, with a `1/3` pager when there are several questions. For multiSelect, taps toggle options and **Submit** appears.
- **R-28** **Terminal** (and "Other…") dismiss the prompt on the bar so the user can answer in the terminal. If the §6 spike fails, the options are shown **read-only**.

### 4.6 Multiple sessions and attention
- **R-30** The session to display is chosen by priority: NeedsInput (oldest first), then Error, then Working (most recent), then Done, then Idle.
- **R-31** The chip shows `+N` when N other sessions need input. Prompts are answered one at a time in FIFO order across sessions.
- **R-32** When the user cycles manually, that choice holds for 10 s or until a new NeedsInput arrives, whichever comes first.
- **R-33** Dimming: after 60 s idle → dim, after 300 s → off. Any touch or key press wakes the bar. **A new NeedsInput always wakes it to full brightness.**
- **R-34** The T1 drives the Touch Bar backlight from its own ambient light sensor in config 2 too (HID report 5 reads back auto-brightness on; verified on MacBookPro13,3, 2026-09-30), so ctb-bar draws pixels at full strength and only applies idle dimming. (Scaling pixels by the screen backlight was dropped: it dimmed the bar twice.)
- **R-35** When a prompt is resolved from anywhere, the bar returns to status mode within 100 ms.

### 4.7 Fallback notification
- **R-36** If `ctb-bar` is not connected (for example, the stock drivers are active), a permission request produces a critical desktop notification (`org.freedesktop.Notifications`) that tells the user to answer in the terminal. It is **alert-only: it has no Allow/Deny buttons and can never carry a decision.** (v0.3 had Allow/Deny actions. On the GNOME session used for development, notification buttons were activated automatically about 2 s after appearing, and not always the same button, so a notification cannot be trusted as a human decision.) On by default; can be turned off.

---

## 5. Prompt protocol and safety

### 5.1 Permission round trip
1. Claude Code runs `ctb-hook` for `PermissionRequest`. The hook `timeout` in `settings.json` is **120 s**. (Claude Code's default is 600 s; permission prompts never resolve on their own.)
2. `ctb-hook` connects to `ctb.sock` (50 ms timeout), sends `{type:"permission", session_id, tool_use_id, tool_name, tool_input, cwd}`, and waits.
3. The bridge queues the prompt and pushes a layout to `ctb-bar`. The user taps.
4. `ctb-hook` prints the result and exits 0:
   ```json
   {"hookSpecificOutput":{"hookEventName":"PermissionRequest",
     "decision":{"behavior":"allow"}}}
   ```
   On deny, the decision is `{"behavior":"deny","message":"Denied from Touch Bar"}`.
5. If there is **no decision within 110 s** (`prompt_timeout_s`), `ctb-hook` exits 0 with **no output**, so Claude Code shows the normal terminal prompt. The bar switches to "Waiting in terminal".

### 5.2 Render protocol (`/run/ctb/render.sock`, newline-delimited JSON)
- **Bridge → bar:** `{"type":"layout","seq":42,"mode":"status|permission|plan|question|terminal","segments":[…],"buttons":[{"id":"allow","label":"Allow","style":"primary","hold_ms":400}, …],"attention":true}`
- **Bar → bridge:** `{"type":"tap","seq":42,"button":"allow","held_ms":431}` and `{"type":"hello","width":2170,"height":60,"backend":"dfr0"}`

### 5.3 Safety requirements
- **R-40 Fail open.** If the bridge is missing, the connection times out, a protocol error occurs, or the bridge crashes mid-wait, `ctb-hook` exits 0 with no output, and the terminal prompt appears as if CTB were not installed.
- **R-41 No auto-approval.** `allow` needs a tap event whose `held_ms` is at least the button's `hold_ms`. **The bridge re-checks `held_ms` itself** rather than trusting the bar's UI state.
- **R-42 Stale taps are ignored.** Every tap carries the layout `seq`. The bridge drops a tap if `seq` is not the current prompt's layout. For 300 ms after a layout change, taps on the changed buttons are ignored, to stop a finger landing on a button that just moved.
- **R-43 Races with the terminal.** If the user answers in the terminal first, the bridge clears the prompt on the next `PostToolUse`, `PostToolUseFailure`, `Stop` or `UserPromptSubmit` for that session, or when the hook's socket closes. **[M0]** Confirm whether Claude Code terminates the hook in that case.
- **R-44 Sockets.** `ctb.sock` is `0600` and checks the peer UID. `render.sock` is `0660`, group `ctb`, and checks peer credentials. There are no TCP sockets.
- **R-45 Privacy.** Nothing is written to disk by default. The opt-in debug log redacts `tool_input` beyond 80 characters.
- **R-46** A hook request gets at most one reply, and a decision reaches exactly one `tool_use_id`.
- **R-47 Touch noise.** Barkeep infers release from a gap in reports, so a hold only counts if the reports are continuous: no gap over 120 ms, and X stays within ±40 px of the start.

---

## 6. AskUserQuestion answering: M0 spike
The hooks docs do not document a supported way to *answer* AskUserQuestion from a hook. The spike must try these, in order:
1. `PreToolUse` (matcher `AskUserQuestion`) returning `permissionDecision:"allow"` plus an `updatedInput` with pre-filled answers.
2. `PreToolUse` returning `deny` with `permissionDecisionReason: "User answered: <option>"`. Check the UX, since Claude sees it as a denial.
3. If neither works: read-only mode (R-28).

---

## 7. Install and configuration

### 7.1 Install
- **R-50** `ctb install` does the following:
  1. Builds and installs the barkeep kernel modules (`barkeep-cfgsel`, `barkeep-dfr`) through **DKMS**.
  2. Installs `ctb-bar.service` (system), the sleep hook (R-04), the `ctb` group and a udev rule.
  3. Installs `ctb-bridge.service` (systemd user unit).
  4. Backs up `~/.claude/settings.json`, then merges in the hook and `statusLine` entries, keeping existing ones.
  5. Does **not** blacklist the stock drivers permanently. `ctb-bar.service` unloads them on start (`ExecStartPre`) and reloads them on stop (R-03), so a broken CTB always leaves you with a working firmware strip after a reboot, or after `systemctl disable ctb-bar`.
- **R-51** `ctb uninstall` reverses every change exactly. `ctb doctor` checks the modules, the `/dev/dfr0` size, touch calibration, the sockets, the services, and the settings entries.
- **R-52** If a statusLine command already exists (e.g. claude-pulse), `ctb-status` wraps it and passes its output through unchanged.
- **R-53** `ctb probe touch /dev/hidrawN` dumps raw digitizer reports so the X offset and range can be found and written to `bar.toml` (a guided `ctb calibrate` is v2).

### 7.2 Config files: `~/.config/ctb/config.toml` (bridge) and `/etc/ctb/bar.toml` (bar)
```toml
# bridge
[prompts]
prompt_timeout_s  = 110        # must be < hook timeout (120)
hold_ms           = 400
dangerous_hold_ms = 1000
always_button     = false
always_scope      = "session"
dangerous = ["rm -rf", "sudo ", "git push --force", "git push -f", "git reset --hard", "| sh", "mkfs", "dd "]

[display]
segments = ["chip", "activity", "ctx", "5h", "7d", "cost", "model"]
done_linger_s = 3

[thresholds]
warn = 70
crit = 90

[fallback]
notifications = true

[statusline]
passthrough = ""               # e.g. path to claude-pulse's claude_status.py
```
```toml
# /etc/ctb/bar.toml
backend        = "dfr0"        # dfr0 | drm
default_layer  = "claude"      # claude | fkeys | media
control_strip  = ["brightness", "volume", "mute", "playpause"]
dim_after_s    = 60
off_after_s    = 300
font           = "Cantarell 20"
```
- **R-54** Config is hot-reloaded when the file changes.

---

## 8. Non-functional requirements
| ID | Requirement |
|---|---|
| R-60 | A non-blocking `ctb-hook` call adds **under 20 ms** at p95. |
| R-61 | **Under 100 ms** from event to pixels (p95), and under 50 ms from touch to function-key event. |
| R-62 | Idle: about 0% CPU for both daemons, under 20 MB RSS each. No redraws while nothing changes. |
| R-63 | After a bridge restart, sessions reappear on their next event. After a `ctb-bar` restart, the bridge resends the current layout on `hello`. |
| R-64 | At least 8 concurrent sessions without dropped events. |
| R-65 | No network access in any component. |
| R-66 | The function row always works, even when the bridge is down, Claude Code isn't running, or the configuration is broken. |

---

## 9. Milestones
| M | Deliverable | Exit criterion |
|---|---|---|
| **M0 (hardware)** | On 13,3: install barkeep and confirm the `/dev/dfr0` size, color order, rotation and touch range. Test suspend with the sleep hook. Check ALS and Fn (`KEY_FN`) visibility. If any of these fail, evaluate backend B. | Test pattern and touch coordinates work; suspend/resume works 10× |
| **M0 (software)** | `PermissionRequest` round trip, the AskUserQuestion spike (§6), the terminal-wins race (R-43) | Written findings |
| **M1** | `ctb-bar` with the function row and control strip only (it replaces the firmware strip) | R-01…R-07, R-17…R-19, R-66 pass |
| **M2** | Bridge, hooks, status mode for one session | R-13…R-16 pass |
| **M3** | Permission and plan prompts, hold, timeout, fail-open, notification fallback | R-20…R-26, R-36, R-40…R-47 pass |
| **M4** | Metrics, multiple sessions, attention, question mode | R-10…R-12, R-27…R-35 pass |
| **M5** | Installer, doctor, calibrate, config | R-50…R-54 pass |
| v2 | Jump to terminal, sounds, custom icons, other agents | – |

---

## 10. Acceptance criteria
| Test | Covers | Pass condition |
|---|---|---|
| T-01 | R-01–R-03 | `systemctl stop ctb-bar`: the firmware strip returns within 5 s. `start`: CTB takes over again. |
| T-04 | R-04 | 10 suspend/resume cycles without a hang. After resume, CTB is back within 5 s. |
| T-05 | R-02, R-18, R-66 | Holding Fn shows F1–F12, and tapping F5 delivers `KEY_F5` to the focused app. Brightness and volume work with the bridge stopped. |
| T-07 | R-07, R-62 | Idle for 60 s: 0 frames sent. Spinner running: at most 10 fps. |
| T-10 | R-10–R-12 | A payload without 7d shows no `7d`. ctx 75% is yellow, 92% red. Metrics older than 60 s are dimmed. |
| T-13 | R-13–R-16 | Submitting a prompt shows a blue spinner and timer. A Bash tool shows `⚙ Bash: <cmd>`. Tapping the chip cycles sessions. At narrow width, model/branch is dropped first. |
| T-17 | R-17, R-19 | With no sessions, the bar shows the media/brightness keys. `default_layer="fkeys"` shows F-keys. |
| T-20 | R-20–R-21 | Asking Claude to run `touch x` shows Allow/Deny within 100 ms. A 200 ms tap on Allow does nothing. A 400 ms hold allows it and the command runs. Deny makes Claude report the denial. |
| T-22 | R-22–R-23 | **Always** on `cargo build` means the next call does not prompt. `rm -rf build` is red, has no Always button, and needs a 1 s hold. |
| T-24 | R-24, §5.1 | After 110 s untouched, the terminal prompt appears and the bar shows "Waiting in terminal". |
| T-25 | R-25–R-26 | ExitPlanMode shows "Plan ready: <heading>". Approve leaves plan mode. Reject keeps Claude planning. |
| T-27 | R-27–R-28 | A 3-option question: tapping option 2 continues Claude with option 2, or in fallback the options are read-only and Terminal dismisses the prompt. |
| T-30 | R-30–R-32 | With A working and B requesting permission, the bar shows B with `+0`, and A comes back after B is resolved. Two prompts are resolved in FIFO order. |
| T-33 | R-33–R-35 | With the bar off, a permission request wakes it to full brightness. Answering in the terminal returns the bar to status mode within 100 ms of the next hook event. |
| T-36 | R-36 | With `ctb-bar` stopped, a permission request produces a critical alert notification with no action buttons, and nothing on it can resolve the prompt. |
| T-40 | R-40 | `systemctl --user stop ctb-bridge`, then trigger a permission request: the terminal prompt appears normally, with under 100 ms added delay. |
| T-41 | R-41, R-42, R-47 | Replaying a tap with an old `seq` has no effect. A tap with `held_ms` 399 on Allow is refused. A finger sliding onto Allow during a layout change does not fire it. |
| T-43 | R-43 | Answering in the terminal clears the bar prompt, and a later tap is ignored. |
| T-44 | R-44, R-45, R-65 | Another user cannot connect to either socket. `ss -tunlp` shows no listeners. There is no log file by default. |
| T-50 | R-50–R-54 | install → uninstall leaves `settings.json` byte-identical and the stock drivers loaded. An existing claude-pulse status line still renders. Config edits apply live. |
| T-60 | R-60–R-64 | Hook p95 under 20 ms. Event-to-pixel p95 under 100 ms. 8 sessions for 10 minutes with no drops. |

---

## 11. Risks and open questions
| # | Item | Mitigation |
|---|---|---|
| 1 | barkeep is only confirmed on **13,2**. It may not work on 13,3. | Test it first in M0. Backend B (appletbdrm, which lists A1707) needs a patched kernel for touch. |
| 2 | **Suspend hang** with barkeep | The sleep hook swaps back to the stock drivers before suspend (R-04). If that fails, CTB is disabled on battery or lid-close. |
| 3 | The ALS is lost in config 2 | Brightness follows the screen backlight (R-34). |
| 4 | Can AskUserQuestion be answered from a hook? | §6 spike; read-only fallback |
| 5 | Is the hook killed when the terminal answers first? | Clear the prompt on the next event (R-43) |
| 6 | Community kernel modules on a stock Ubuntu kernel (DKMS rebuild on each kernel update) | `ctb doctor` detects a missing module. The stock drivers keep working. |

## References
- barkeep: T1 Touch Bar as a real display (`/dev/dfr0`, touch protocol): https://github.com/mgd34msu/barkeep
- appletbdrm (sunplex07 fork), DRM driver for T1 incl. A1707: https://github.com/sunplex07/appletbdrm
- T1 firmware-strip setup and the hid-sensor-hub regression: https://github.com/michaelahess/macbook-pro-t1-touchbar-linux
- Claude Code hooks reference: https://code.claude.com/docs/en/hooks

---

## 12. Implementation status (v0.4)

Code: `ctb/` (Python 3.10 stdlib + pycairo + PIL), `bin/`, `packaging/`, `tests/` (42 tests), `tools/`.

### Deviations from the spec text
| Spec | As built | Why |
|---|---|---|
| Rust binaries | **Python 3** | No Rust toolchain on the machine; installing one is a system change that was not asked for. `ctb-hook` avoids the `json`/`socket` imports on its hot path: **13 ms avg, 15.6 ms p95** (bare `python3 -S` is 9.7 ms), inside R-60. |
| `render.sock` in `/run/ctb`, group `ctb` | `$XDG_RUNTIME_DIR/ctb-render.sock`, mode `0600`, owned by the user. `ctb-bar` (root) connects **to** the bridge as a client and retries every 2 s | The bridge is a user service and cannot create `/run/ctb`. Peer UID must be the user or root. No `ctb` group is needed. |
| R-36 notification Allow/Deny | **Alert only** | See R-36. |
| Single config file per side | `~/.config/ctb/config.toml` (bridge) and `/etc/ctb/bar.toml` (bar), same TOML subset parser (Python 3.10 has no `tomllib`) | As spec §7.2. |
| R-16 "tap activity shows metrics" | Not implemented | Low value; the metrics are already on the bar when they fit. |

### Status per area
| Area | State |
|---|---|
| Hook events, state machine, tool summaries, metrics (R-10…R-14, R-30…R-35) | **Done, tested** (unit + integration) |
| Permission / plan prompts, hold, guard, stale taps, timeout, terminal-wins race, fail-open (R-20…R-26, R-40…R-47) | **Done, tested**, and **verified against real Claude Code 2.1.285** in `-p` mode: allow runs the command, deny blocks it and Claude reports the denial (`tools/e2e_claude.py`) |
| `statusLine` field names (§3.2) | **Verified against the Claude Code docs** (`context_window.used_percentage`, `rate_limits.five_hour/seven_day`, `cost.total_cost_usd`, `model.display_name`, `effort.level`, `workspace.current_dir`) |
| Renderer, function row, control strip, Fn layer, dimming, hit-testing, hold-progress (§4, R-01…R-07, R-17…R-19) | **Done**, tested with a software backend; layouts checked visually (`tools/preview.py`) |
| Installer / uninstaller / doctor (R-50…R-52) | **Done**, tested against a temp settings file: install ×2 + uninstall is byte-identical; edits made after install survive uninstall |
| Notification alert (R-36) | **Done**, tried live on GNOME |
| **Question mode (§6)** | **Read-only only.** The AskUserQuestion answering spike was not run (it needs an interactive session) |
| **Hardware layer**: `Dfr0Backend` (frame rotation), `HidrawTouch` (report format, X offset/flip), `FnWatcher` (`KEY_FN`), `UinputKeys`, barkeep on 13,3, suspend hook | **Written but UNVERIFIED on the Touch Bar.** Every one of these is an M0 item; see README "M0 checklist" |
| Backend B (DRM / appletbdrm) | Not implemented (only needed if barkeep fails on 13,3) |
| `AskUserQuestion` blocking, `ctb calibrate`, jump-to-terminal, sounds | Not done (v2) |
