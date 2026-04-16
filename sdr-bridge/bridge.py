#!/usr/bin/env python3
"""
SDR Bridge — WebSocket server proxying Chrome to rigctld.
Supports SDR++ (freq/mode only) and gqrx (freq/mode/bw/strength/squelch).
No third-party dependencies.
"""
import base64, hashlib, json, socket, struct, subprocess, sys, threading, time, logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    stream=sys.stdout)
log = logging.getLogger('sdr-bridge')

CONFIG_PATH = '/var/sdr-bridge/config.json'
DEFAULTS    = {
    'host': '192.168.7.1', 'port': 0, 'rig': 'auto',
    'bl_mode': 'auto', 'bl_brightness': 128,
}
RIG_PORTS   = {'sdrpp': 4532, 'gqrx': 7356}
WS_HOST     = '127.0.0.1'
WS_PORT     = 8080
POLL_HZ     = 5

# Backlight + proximity sysfs (Car Thing tmd2772 + aml-bl)
BL_PATH        = '/sys/class/backlight/aml-bl/brightness'
BL_MAX         = 255
BL_SUPERVISOR  = 'backlight'   # supervisord program name for sp-als-backlight
PROX_DEV       = '/sys/devices/platform/soc/ffd00000.cbus/ffd1d000.i2c/i2c-2/2-0039/iio:device0'
PROX_RAW       = PROX_DEV + '/in_proximity0_raw'
PROX_EVENT_EN  = PROX_DEV + '/events/in_proximity0_thresh_rising_en'
PROX_CALSCALE  = PROX_DEV + '/in_proximity0_calibscale'

_WS_MAGIC = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return {
            'host':          str(cfg.get('host',          DEFAULTS['host'])),
            'port':          int(cfg.get('port',          DEFAULTS['port'])),
            'rig':           str(cfg.get('rig',           DEFAULTS['rig'])),
            'bl_mode':       str(cfg.get('bl_mode',       DEFAULTS['bl_mode'])),
            'bl_brightness': int(cfg.get('bl_brightness', DEFAULTS['bl_brightness'])),
        }
    except Exception:
        return dict(DEFAULTS)

def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f)
        log.info('config saved: %s', cfg)
    except Exception as e:
        log.warning('config save failed: %s', e)


# ── Backlight & proximity ─────────────────────────────────────────────────────

def _supervisorctl(*args):
    try:
        subprocess.run(['supervisorctl'] + list(args),
                       timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warning('supervisorctl %s failed: %s', args, e)

BL_HW_MIN = 1    # hw=0 turns backlight off
BL_HW_MAX = 240  # hw>240 flickers badly

def _ui_to_hw(ui):
    """Hardware brightness is inverted (low hw = bright, 0/255 = off).
    Map UI 0..255 (0=dim, 255=bright) to hw 240..1."""
    ui = max(0, min(BL_MAX, int(ui)))
    return max(BL_HW_MIN, min(BL_HW_MAX, BL_MAX - ui))

def _hw_to_ui(hw):
    hw = max(0, min(BL_MAX, int(hw)))
    return max(0, min(BL_MAX, BL_MAX - hw))

_bl_daemon_running = None  # tri-state: None=unknown, True/False=known

def apply_backlight(mode, brightness):
    """Switch between the sp-als-backlight daemon (auto) and a manual value.
    Only runs supervisorctl on actual mode transitions to keep manual drags cheap."""
    global _bl_daemon_running
    want_daemon = (mode != 'manual')
    if _bl_daemon_running != want_daemon:
        _supervisorctl('start' if want_daemon else 'stop', BL_SUPERVISOR)
        _bl_daemon_running = want_daemon
        log.info('backlight daemon -> %s', 'on' if want_daemon else 'off')
    if mode == 'manual':
        hw = _ui_to_hw(brightness)
        try:
            with open(BL_PATH, 'w') as f:
                f.write(str(hw))
        except Exception as e:
            log.warning('backlight write failed: %s', e)

def read_backlight():
    try:
        with open(BL_PATH) as f:
            return _hw_to_ui(int(f.read().strip()))
    except Exception:
        return None

def init_prox():
    """One-shot setup: enable continuous prox measurements on the tmd2772."""
    for path, val in ((PROX_EVENT_EN, '1'), (PROX_CALSCALE, '1')):
        try:
            with open(path, 'w') as f:
                f.write(val)
        except Exception as e:
            log.warning('prox init (%s) failed: %s', path, e)

def read_prox():
    try:
        with open(PROX_RAW) as f:
            return int(f.read().strip())
    except Exception:
        return None


# ── WebSocket helpers ─────────────────────────────────────────────────────────

def _ws_accept(conn):
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = conn.recv(2048)
        if not chunk:
            return False
        buf += chunk
    for line in buf.decode('utf-8', errors='replace').split('\r\n'):
        if line.lower().startswith('sec-websocket-key:'):
            key = line.split(':', 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((key + _WS_MAGIC).encode()).digest()
            ).decode()
            conn.sendall((
                'HTTP/1.1 101 Switching Protocols\r\n'
                'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                'Sec-WebSocket-Accept: {}\r\n\r\n'.format(accept)
            ).encode())
            return True
    return False


def _ws_recv(conn):
    def exact(n):
        buf = b''
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf
    hdr = exact(2)
    if hdr is None:
        return None, None
    opcode = hdr[0] & 0x0f
    masked = bool(hdr[1] & 0x80)
    length = hdr[1] & 0x7f
    if length == 126:
        ext = exact(2)
        if ext is None: return None, None
        length = struct.unpack('>H', ext)[0]
    elif length == 127:
        ext = exact(8)
        if ext is None: return None, None
        length = struct.unpack('>Q', ext)[0]
    mask = exact(4) if masked else None
    data = exact(length)
    if data is None:
        return None, None
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


def _ws_send(conn, text):
    data = text.encode('utf-8')
    n = len(data)
    if n < 126:
        header = bytes([0x81, n])
    elif n < 65536:
        header = struct.pack('>BBH', 0x81, 126, n)
    else:
        header = struct.pack('>BBQ', 0x81, 127, n)
    conn.sendall(header + data)


# ── Rig client ────────────────────────────────────────────────────────────────

class Rig:
    def __init__(self):
        self._sock  = None
        self._buf   = b''
        self._lock  = threading.Lock()
        self.ok     = False
        self.host   = DEFAULTS['host']
        self.port   = DEFAULTS['port']
        self.cfg_type = DEFAULTS['rig']   # 'auto' | 'sdrpp' | 'gqrx' — user's choice
        self.type     = None              # 'sdrpp' | 'gqrx' — actually active (after connect)

    def configure(self, host, port, rig_type):
        self.host = host
        self.port = port
        self.cfg_type = rig_type
        self.type = None
        self.disconnect()

    def _attempts(self):
        """List of (rig_type, port) pairs to try when connecting."""
        if self.cfg_type == 'auto':
            return [('gqrx', RIG_PORTS['gqrx']), ('sdrpp', RIG_PORTS['sdrpp'])]
        port = self.port if self.port else RIG_PORTS.get(self.cfg_type, 4532)
        return [(self.cfg_type, port)]

    def connect(self):
        for rig_type, port in self._attempts():
            try:
                s = socket.socket()
                s.settimeout(2)
                s.connect((self.host, port))
                s.settimeout(2)
                with self._lock:
                    self._sock = s
                    self._buf  = b''
                    self.ok    = True
                    self.type  = rig_type
                    self.port  = port
                log.info('connected to %s at %s:%d', rig_type, self.host, port)
                return True
            except Exception as e:
                if self.cfg_type == 'auto':
                    log.debug('auto-probe %s:%d failed: %s', rig_type, port, e)
                else:
                    log.warning('connect failed: %s', e)
        self.ok = False
        self.type = None
        return False

    def disconnect(self):
        with self._lock:
            self.ok = False
            if self._sock:
                try: self._sock.close()
                except: pass
                self._sock = None
                self._buf  = b''

    def _readline(self):
        while b'\n' not in self._buf:
            try:
                chunk = self._sock.recv(512)
                if not chunk:
                    return None
                self._buf += chunk
            except Exception:
                return None
        line, self._buf = self._buf.split(b'\n', 1)
        return line.decode('utf-8', errors='replace').strip()

    def _cmd(self, command, nlines=1):
        with self._lock:
            if not self.ok:
                return None
            try:
                self._sock.sendall((command + '\n').encode())
                lines = []
                for _ in range(nlines):
                    line = self._readline()
                    if line is None:
                        self.ok = False
                        return None
                    lines.append(line)
                return lines
            except Exception as e:
                log.warning('cmd %r error: %s', command, e)
                self.ok = False
                return None

    def _ok(self, r):
        return r and not r[0].startswith('RPRT')

    # ── get/set ───────────────────────────────────────────────────────────────

    def get_freq(self):
        r = self._cmd('f')
        if r:
            try: return int(float(r[0]))
            except: pass
        return None

    def set_freq(self, hz):
        self._cmd('F {}'.format(int(hz)))

    def get_mode(self):
        """Returns (mode_str, bw_hz_or_None)."""
        if self.type == 'gqrx':
            r = self._cmd('m', nlines=2)
            if r:
                mode = r[0]
                try: bw = int(r[1])
                except: bw = None
                return mode, bw
        else:
            r = self._cmd('m', nlines=1)
            if r:
                return r[0], None
        return None, None

    def set_mode(self, mode, passband=0):
        self._cmd('M {} {}'.format(mode, int(passband)))

    def get_strength(self):
        if self.type != 'gqrx':
            return None
        r = self._cmd('l STRENGTH')
        if self._ok(r):
            try: return float(r[0])
            except: pass
        return None

    def get_sql(self):
        if self.type != 'gqrx':
            return None
        r = self._cmd('l SQL')
        if self._ok(r):
            try: return float(r[0])
            except: pass
        return None

    def set_sql(self, val):
        if self.type == 'gqrx':
            self._cmd('L SQL {:.1f}'.format(float(val)))

    def get_af(self):
        if self.type != 'gqrx':
            return None
        r = self._cmd('l AF')
        if self._ok(r):
            try: return float(r[0])
            except: pass
        return None

    def set_af(self, val):
        if self.type == 'gqrx':
            # gqrx AF is audio gain in dB (roughly -80 to +50)
            self._cmd('L AF {:.1f}'.format(max(-80.0, min(50.0, float(val)))))

    def _get_parm(self, name):
        if self.type != 'gqrx':
            return None
        r = self._cmd('u ' + name)
        if self._ok(r):
            try: return bool(int(r[0]))
            except: pass
        return None

    def _set_parm(self, name, on):
        if self.type == 'gqrx':
            self._cmd('U {} {}'.format(name, 1 if on else 0))

    def get_rec(self): return self._get_parm('RECORD')
    def set_rec(self, on): self._set_parm('RECORD', on)
    def get_dsp(self): return self._get_parm('DSP')
    def set_dsp(self, on): self._set_parm('DSP', on)


# ── Bridge ────────────────────────────────────────────────────────────────────

class Bridge:
    def __init__(self):
        self._rig     = Rig()
        self._clients = []
        self._clock   = threading.Lock()
        cfg = load_config()
        self._cfg = cfg
        self._rig.host     = cfg['host']
        self._rig.port     = cfg['port']
        self._rig.cfg_type = cfg['rig']
        self._state = {
            'freq': 14200000, 'mode': 'FM', 'bw': None,
            'strength': None, 'sql': None,
            'af': None, 'rec': None, 'dsp': None,
            'rig_ok': False,
            'cfg_host': cfg['host'], 'cfg_port': cfg['port'], 'cfg_rig': cfg['rig'],
            'rig': None,  # active detected type (null when disconnected)
            'bl_mode': cfg['bl_mode'], 'bl_brightness': cfg['bl_brightness'],
            'prox': None,
        }
        apply_backlight(cfg['bl_mode'], cfg['bl_brightness'])
        init_prox()

    def _broadcast(self, msg):
        text = json.dumps(msg)
        dead = []
        with self._clock:
            for c in self._clients:
                try: _ws_send(c, text)
                except: dead.append(c)
            for c in dead:
                try: c.close()
                except: pass
                self._clients.remove(c)

    def _handle_client(self, conn, addr):
        log.info('WS connect %s', addr)
        try:
            if not _ws_accept(conn):
                return
            _ws_send(conn, json.dumps(self._state))
            with self._clock:
                self._clients.append(conn)
            while True:
                opcode, data = _ws_recv(conn)
                if opcode is None or opcode == 8:
                    break
                if opcode == 1 and data:
                    try: self._handle_cmd(json.loads(data.decode('utf-8')))
                    except: pass
        except Exception as e:
            log.warning('client %s error: %s', addr, e)
        finally:
            with self._clock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try: conn.close()
            except: pass
            log.info('WS disconnect %s', addr)

    def _handle_cmd(self, msg):
        cmd = msg.get('cmd')
        if cmd == 'set_freq':
            hz = int(msg['freq'])
            self._rig.set_freq(hz)
            self._state['freq'] = hz
        elif cmd == 'set_mode':
            mode = str(msg['mode'])
            bw   = int(msg.get('bw') or msg.get('passband') or 0)
            self._rig.set_mode(mode, bw)
            self._state['mode'] = mode
            if bw: self._state['bw'] = bw
        elif cmd == 'set_bw':
            bw = int(msg['bw'])
            self._rig.set_mode(self._state['mode'], bw)
            self._state['bw'] = bw
        elif cmd == 'set_sql':
            val = float(msg['sql'])
            self._rig.set_sql(val)
            self._state['sql'] = val
        elif cmd == 'set_af':
            val = float(msg['af'])
            self._rig.set_af(val)
            self._state['af'] = val
        elif cmd == 'set_rec':
            on = bool(msg['rec'])
            self._rig.set_rec(on)
            self._state['rec'] = on
            self._broadcast(dict(self._state))
        elif cmd == 'set_dsp':
            on = bool(msg['dsp'])
            self._rig.set_dsp(on)
            self._state['dsp'] = on
            self._broadcast(dict(self._state))
        elif cmd == 'set_config':
            host = str(msg['host'])
            port = int(msg['port'])
            rig  = str(msg.get('rig', 'sdrpp'))
            self._cfg.update({'host': host, 'port': port, 'rig': rig})
            save_config(self._cfg)
            self._state.update({'cfg_host': host, 'cfg_port': port, 'cfg_rig': rig})
            self._rig.configure(host, port, rig)
            log.info('reconnecting to %s at %s:%d', rig, host, port)
        elif cmd == 'set_backlight':
            mode       = str(msg.get('mode', 'auto'))
            brightness = int(msg.get('brightness', self._cfg['bl_brightness']))
            self._cfg.update({'bl_mode': mode, 'bl_brightness': brightness})
            save_config(self._cfg)
            self._state.update({'bl_mode': mode, 'bl_brightness': brightness})
            apply_backlight(mode, brightness)
            self._broadcast(dict(self._state))

    def _poll_loop(self):
        retry_delay = 1.0
        tick = 0
        while True:
            rig = self._rig
            rig_was_down = not rig.ok
            if rig_was_down:
                if rig.connect():
                    retry_delay = 1.0
                    self._state['cfg_port'] = rig.port  # auto-detect may have picked a port
                else:
                    self._state['rig_ok'] = False
                    self._state['rig']    = None
                    self._poll_sensors(tick)
                    self._broadcast(dict(self._state))
                    time.sleep(min(retry_delay, 10.0))
                    retry_delay = min(retry_delay * 1.5, 10.0)
                    tick += 1
                    continue

            freq         = rig.get_freq()
            mode, bw     = rig.get_mode()
            strength     = rig.get_strength()
            sql          = rig.get_sql()

            if freq     is not None: self._state['freq']     = freq
            if mode     is not None: self._state['mode']     = mode
            if bw       is not None: self._state['bw']       = bw
            if strength is not None: self._state['strength'] = strength
            if sql      is not None: self._state['sql']      = sql

            # audio params at 1 Hz — not touched by user often, cheap to poll
            if tick % POLL_HZ == 0:
                af  = rig.get_af()
                rec = rig.get_rec()
                dsp = rig.get_dsp()
                if af  is not None: self._state['af']  = af
                if rec is not None: self._state['rec'] = rec
                if dsp is not None: self._state['dsp'] = dsp

            self._state['rig_ok'] = rig.ok
            self._state['rig']    = rig.type
            self._poll_sensors(tick)
            self._broadcast(dict(self._state))
            time.sleep(1.0 / POLL_HZ)
            tick += 1

    def _poll_sensors(self, tick):
        # brightness every ~1s in auto mode (daemon updates it)
        if self._state.get('bl_mode') == 'auto' and tick % POLL_HZ == 0:
            b = read_backlight()
            if b is not None:
                self._state['bl_brightness'] = b

    def _prox_loop(self):
        """Poll the prox sensor in its own thread for snappy UI wake/idle.
        Broadcast immediately when crossing the wake threshold; rely on the
        main poll broadcast otherwise to avoid flooding the WebSocket."""
        PROX_HZ    = 15
        WAKE_LEVEL = 60
        prev_near  = False
        while True:
            p = read_prox()
            if p is not None:
                self._state['prox'] = p
                near = p >= WAKE_LEVEL
                if near != prev_near:
                    self._broadcast(dict(self._state))
                prev_near = near
            time.sleep(1.0 / PROX_HZ)

    def run(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._prox_loop, daemon=True).start()
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((WS_HOST, WS_PORT))
        srv.listen(4)
        log.info('WS server on %s:%d', WS_HOST, WS_PORT)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self._handle_client,
                             args=(conn, addr), daemon=True).start()


if __name__ == '__main__':
    Bridge().run()
