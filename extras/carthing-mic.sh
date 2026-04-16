#!/bin/sh
# carthing-mic.sh — turn the Car Thing into a USB microphone.
#
# Adds the Android `audio_source` gadget function to the running USB gadget
# and pipes the built-in mic (ALSA hw:0,0) into it. The host PC sees a
# no-driver USB recording device.
#
# Usage (from the device, detached so it survives the brief USB rebind):
#   nohup /var/carthing-mic.sh start >/dev/null 2>&1 &
#   /var/carthing-mic.sh stop
#
# Or pushed + launched from the host:
#   adb push carthing-mic.sh /var/carthing-mic.sh
#   adb shell chmod +x /var/carthing-mic.sh
#   adb shell 'nohup /var/carthing-mic.sh start >/dev/null 2>&1 &'
#
# Note: ADB & RNDIS will briefly disconnect while the gadget rebinds.

G=/sys/kernel/config/usb_gadget/g1
FN=$G/functions/audio_source.0
LINK=$G/configs/c.1/audio_source.0
LOG=/var/log/carthing-mic.log
PIDFILE=/var/run/carthing-mic.pid

RATE=44100       # audio_source gadget is hardwired to 44.1 kHz
CHANNELS=2
FORMAT=S16_LE
MIC=hw:0,0       # AML-AUGESOUND capture

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >> "$LOG"; }

find_sink() {
    # Any ALSA card whose id is not the built-in AUGESOUND card
    for i in 0 1 2 3 4 5; do
        [ -d /proc/asound/card$i ] || continue
        id=$(cat /proc/asound/card$i/id 2>/dev/null)
        case "$id" in
            *AUGE*|*auge*) : ;;
            *) echo "hw:$i,0"; return 0 ;;
        esac
    done
    return 1
}

start() {
    if [ -e "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
        log "already running pid=$(cat $PIDFILE)"; return 0
    fi

    # Re-exec under setsid with FDs closed so we survive the ADB/USB drop
    # that happens when we unbind the UDC.
    if [ -z "$CARTHING_MIC_DETACHED" ]; then
        CARTHING_MIC_DETACHED=1 setsid "$0" start </dev/null >>"$LOG" 2>&1 &
        return 0
    fi

    if [ ! -e "$LINK" ]; then
        UDC=$(cat "$G/UDC")
        log "rebinding gadget: add audio_source (UDC=$UDC)"
        echo "" > "$G/UDC"
        mkdir -p "$FN"
        ln -s "$FN" "$LINK"
        echo "$UDC" > "$G/UDC"
        sleep 3   # wait for host to re-enumerate
    fi

    SINK=$(find_sink) || { log "no audio_source card appeared"; return 1; }
    log "piping $MIC -> $SINK ($RATE Hz $CHANNELS ch $FORMAT)"

    arecord -D "$MIC"  -f $FORMAT -r $RATE -c $CHANNELS -t raw --buffer-size=4096 2>>"$LOG" \
      | aplay  -D "$SINK" -f $FORMAT -r $RATE -c $CHANNELS -t raw              2>>"$LOG" &
    echo $! > "$PIDFILE"
    log "started pipe pid=$!"
    wait $!
}

stop() {
    if [ -e "$PIDFILE" ]; then
        kill "$(cat $PIDFILE)" 2>/dev/null || true
        rm -f "$PIDFILE"
        log "killed pipe"
    fi
    if [ -e "$LINK" ]; then
        UDC=$(cat "$G/UDC")
        log "removing audio_source, rebinding gadget"
        echo "" > "$G/UDC"
        rm -f "$LINK"
        rmdir "$FN" 2>/dev/null || true
        echo "$UDC" > "$G/UDC"
    fi
    log "stopped"
}

status() {
    if [ -e "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
        echo "running pid=$(cat $PIDFILE)"
    else
        echo "stopped"
    fi
    [ -e "$LINK" ] && echo "gadget: audio_source attached" || echo "gadget: audio_source not attached"
}

case "${1:-start}" in
    start)  start ;;
    stop)   stop ;;
    status) status ;;
    *) echo "Usage: $0 {start|stop|status}"; exit 1 ;;
esac
