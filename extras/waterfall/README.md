# Audio Waterfall

A minimal, standalone FFT waterfall display fed from gqrx's or SDR++'s UDP
audio stream. Completely separate from carthing-vfo — two files, no deps.

- `waterfall-bridge.py` — listens for UDP audio on :7355, rebroadcasts each
  datagram as a binary WebSocket frame on :8081.
- `waterfall.html` — connects to the WS, Hann-windows + FFTs incoming
  int16 PCM, and paints a scrolling waterfall on a full-window canvas.

## Quick start (on the host running gqrx / SDR++)

```sh
python3 waterfall-bridge.py
xdg-open waterfall.html   # or just drag it into a browser
```

Then enable UDP streaming in your SDR app:

- **gqrx** — *Tools → UDP Controller*. Host `127.0.0.1`, Port `7355`,
  press Start. Streams mono int16 PCM @ 48 kHz by default.
- **SDR++** — add the *Network sink* (or the Audio sink with `Network` mode),
  Protocol `UDP`, Host `127.0.0.1`, Port `7355`, format `Int16`.

## Running on the Car Thing instead

```sh
adb push waterfall-bridge.py /var/waterfall-bridge.py
adb push waterfall.html      /var/waterfall.html
adb shell python3 /var/waterfall-bridge.py &
```

Point gqrx's / SDR++'s UDP output at `192.168.7.2:7355` (the Car Thing's
RNDIS address). To view it on the device, stop the main webapp and relaunch
Chromium with `--app=file:///var/waterfall.html`, or just open it from a
desktop browser pointed at `ws://192.168.7.2:8081`.

## Knobs in `waterfall.html`

Top of the `<script>` block:

- `FFT_N`        — FFT size (power of two). 1024 = good balance.
- `SAMPLE_RATE`  — labeling only; the FFT doesn't care
- `UPDATE_HZ`    — max rows per second drawn
- `DB_MIN / DB_MAX` — colormap range; adjust if the display looks flat or
  saturated
