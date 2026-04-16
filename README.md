# carthing-vfo

SDR controller UI for the Spotify Car Thing. Runs a minimal WebSocket bridge (`sdr-bridge/bridge.py`) on the device that speaks rigctld to SDR++ or gqrx on the host machine, and serves a fullscreen frequency-control interface (`webapp/index.html`) in Chromium kiosk mode on the Car Thing's 800×480 display.

## Hardware Requirements

- Spotify Car Thing with USB/ADB access (superbird firmware, supervisord running)
- Host machine running SDR++ or gqrx with rigctld enabled
- USB connection between Car Thing and host (provides both ADB and the 192.168.7.x network link)

Tested against the [Thing Labs 8.9.2 firmware](https://thingify.tools/firmware/P3QZbZIDWnp5m_azQFQqP), flashed with [Terbium](https://terbium.app/).

## Installation

### 1. ADB Prerequisites

Install ADB on the host if needed (`apt install adb` / `brew install android-platform-tools`), then connect via USB:

```sh
adb devices
```

The Car Thing should appear in the device list.

### 2. Remount Root Read-Write

```sh
adb shell mount -o remount,rw /
```

### 3. Create Directories

```sh
adb shell mkdir -p /var/sdr-bridge /var/webapp
```

### 4. Push Files

```sh
adb push sdr-bridge/bridge.py /var/sdr-bridge/bridge.py
adb push webapp/index.html    /var/webapp/index.html
```

### 5. Write Initial Config

```sh
adb shell 'echo "{\"host\":\"192.168.7.1\",\"port\":0,\"rig\":\"auto\",\"bl_mode\":\"auto\",\"bl_brightness\":128}" > /var/sdr-bridge/config.json'
```

With `rig: "auto"` (the default) the bridge probes gqrx (port 7356) first, then SDR++ (port 4532), and uses whichever responds. Set `rig` to `"sdrpp"` or `"gqrx"` and `port` to a specific value to skip auto-detection. `bl_mode` / `bl_brightness` control the backlight and can also be changed live from the settings panel.

### 6. Edit supervisord.conf on Device

```sh
adb shell
vi /etc/supervisord.conf
```

**Change the Chromium `--app=` flag** to point at the local webapp:

```ini
command=chromium ... --app=file:///var/webapp/index.html ...
```

**Add the sdr-bridge program block** (anywhere in the file):

```ini
[program:sdr-bridge]
command=/usr/bin/python3 /var/sdr-bridge/bridge.py
autostart=true
autorestart=true
stdout_logfile=/var/log/sdr-bridge.log
stderr_logfile=/var/log/sdr-bridge.log
```

### 7. Restart Services

```sh
adb shell supervisorctl restart sdr-bridge chromium
```

## gqrx Setup

In gqrx, open **Tools → Remote Control Settings** and add the Car Thing's IP (`192.168.7.2`) to the allowed clients list, then enable remote control. The bridge connects outbound from the device (`192.168.7.2`) to the host (`192.168.7.1`) on port 7356 (gqrx's default).

## SDR++ Setup

Enable the rigctld server plugin in SDR++, then change the **Listen Address** from `127.0.0.1` to `0.0.0.0` (or `192.168.7.1`, the USB network IP of the host). By default SDR++ only listens on localhost and the Car Thing cannot reach it. The default SDR++ port is 4532.

## UI Overview

Top to bottom:

- **Top bar** — step size, rig/host/port status badge. Tap the badge (or long-press the M button) to open settings; swipe down to open the preset popup.
- **Frequency display** — GHz.MHz.kHz.Hz, one digit per position. The digit matching the current step glows green; others are dim. Wraps at 10 GHz.
- **Mode row** — demodulator buttons. For gqrx, tapping a grouped button (AM / CW / WFM) cycles through its sub-modes (AM↔AMS, CWL↔CWU, WFM→ST→OIRT).
- **BW / SQL row** — gqrx only. Swipe or use ± buttons. Swipe a larger distance = faster change.
- **dB meter** — gqrx only, −100 to 0 dB scale. An amber line shows the current squelch threshold.
- **Audio panel** — gqrx only. Contains audio gain slider (VOL, −80 to +50 dB), REC toggle (WAV recording), and DSP toggle. Hidden by default; see auto-hide behavior below.

## Controls Reference

| Input | Action |
|-------|--------|
| Knob rotate | Tune frequency by current step size |
| Knob press (Enter) | Step size up; hold to cycle continuously |
| Back button (Esc) | Step size down; hold to cycle continuously |
| Buttons 1–4 short press | Recall preset (popup briefly shown) |
| Buttons 1–4 hold (~700 ms) | Save current freq / mode / BW as preset |
| M button short press | Cycle demodulation mode |
| M button hold (~700 ms) | Open settings |
| Tap frequency digit | Set step to that digit's place value |
| Swipe left/right on frequency | Tune (one step per 24 px) |
| Swipe left/right on BW half | Adjust bandwidth (gqrx only) |
| Swipe left/right on SQL half | Adjust squelch (gqrx only) |
| Swipe down from top bar | Show preset popup |
| Swipe up from bottom edge | Toggle audio panel (when PROX HIDE is off) |
| Tap status badge (top right) | Open settings |

## Settings Panel

Accessed by tapping the status badge or holding M. Two tabs:

### RIG
- **RIG** — AUTO / SDR++ / GQRX. AUTO probes both default ports on each reconnect.
- **HOST** — four octet spinners.
- **PORT** — spinner (disabled when RIG is AUTO; shows "AUTO" in that case).
- **APPLY** commits the rig change. Reconnection is automatic.

### CAR THING
- **LIGHT** — AUTO / MANUAL. AUTO delegates to the `sp-als-backlight` daemon (ambient light sensor). MANUAL stops that daemon and drives `/sys/class/backlight/aml-bl/brightness` directly; the slider below is live (no APPLY required).
- **THEME** — AUTO / DARK / LIGHT. AUTO follows the current display brightness (≥ 160 → light, else dark).
- **PROX HIDE** — ON / OFF. Auto-hide behavior driven by the TMD2772 proximity sensor; see below.

## Auto-hide (proximity)

When **PROX HIDE** is on:
- **Idle** (no hand within ~6–8 cm AND no interaction for 3 s): top bar and mode row remain visible; BW/SQL row and audio panel fade away, freeing screen space for the frequency display.
- **Active** (prox wakes, or any touch/knob/key event): all rows reappear, including the audio panel for gqrx.

When **PROX HIDE** is off:
- BW/SQL row always visible. Audio panel hidden until swiped up from the bottom edge; swipe up again (or tap outside) to dismiss.

Presets are stored in `localStorage` and survive page reloads. All settings in the RIG and CAR THING tabs are persisted to `/var/sdr-bridge/config.json` on the device (except the theme, which is per-browser in `localStorage`).

## License

[WTFPL](LICENSE) — do whatever you want with this.
