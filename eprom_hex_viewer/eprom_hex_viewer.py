import os
import re
import json
import shutil
import threading
import queue
import time
import datetime
import csv
import traceback
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

try:
    import serial
    import serial.tools.list_ports
except ImportError:          # the viewer still works without it; only the live port B connection needs pyserial
    serial = None

# Port B protocol (One ROM Ostrich trace firmware):  PC sends "TRACE\n"  ->  device answers "OR-TRACE2\n" and then streams
#   A7 n ts32 addr16*n   (little endian; ts = device uptime ms)       A8 dropped16 overruns16  (health, once a second)
# "STOP\n" (or closing the port) makes the device silent again.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # logs and settings live next to this program
LOG_DIR = os.path.join(BASE_DIR, "eprom_logs")
ERR_LOG = os.path.join(BASE_DIR, "eprom_viewer_error.log")
LOG_HEADER = "seq,time,host_s,dev_ms,addr\n"                  # seq = running number (keeps the order), time = PC clock, host_s = seconds since Record,
                                                              # dev_ms = One ROM uptime when the frame was sent, addr = hex EPROM address


# ---------------------------------------------------------------------------------------------------------------------
# AEM wideband relay (ported from aem_wideband_reader.py).  Only runs while it is switched on:
#   AEM controller (COM, 9600 8N1, RTS/CTS, ASCII "14.7" lines) -> this program -> port B "L1 <n>\n" / "L2 <n>\n"
#   (n = lambda x 1000)  -> One ROM -> TunerPro (lambda items of the Ostrich data packet).
# "WB 1\n" switches the relay on in the firmware, "WB 0\n" off (the firmware then reports no signal to TunerPro).
# ---------------------------------------------------------------------------------------------------------------------
WB_STOICH = 14.7
WB_SEND_HZ = 40                       # max sends per second: each new AEM reading is forwarded at once (event driven), limited to this rate
WB_HEARTBEAT_S = 0.25                 # no new reading: repeat the last value this often
WB_STALE_S = 1.0                      # no new reading for this long -> stop sending (firmware shows "no signal" after 2 s)


def wb_parse_afr(line):
    """'14.7' or ' 14.70\\r' -> 14.7 ; anything else -> None.  Rejects nan/inf and absurd values."""
    try:
        v = float(line.strip())
    except ValueError:
        return None
    if v != v or v <= 0.0 or v > 99.9:
        return None
    return v


def wb_lambda_milli(afr, stoich=WB_STOICH):
    """AFR -> lambda x 1000, clamped to what the firmware accepts (0..65535)."""
    return max(1, min(65535, int(round(afr / stoich * 1000.0))))


# ---------------------------------------------------------------- TunerPro .xdl log -> CSV (same layout as tools/xdl_to_csv.py)
XDL_HDR, XDL_REC, XDL_SHIFT = 187, 102, 29      # file header, record size, (datastream byte N is record byte N-29)
XDL_COLS = ["rec", "t_ms", "RPMAX", "MAFAX", "TPS", "ECT", "IAT", "INJ", "VBAT", "LOAD", "ADV",
            "LAMBDA1_x1000", "LAMBDA2_x1000", "CROW", "CCOL", "ALIVE"]


def xdl_rows(data):
    """Decode the bytes of a TunerPro .xdl log recorded with the One ROM ADX into a list of rows (see XDL_COLS)."""
    n = max(0, (len(data) - XDL_HDR) // XDL_REC)
    rows, t, last = [], 0, None
    for i in range(n):
        r = data[XDL_HDR + i * XDL_REC: XDL_HDR + (i + 1) * XDL_REC]
        g = lambda off: r[off - XDL_SHIFT]
        stamp = r[57]
        if last is not None:
            t += (stamp - last) % 256
        last = stamp
        rows.append([i, t, g(0x3F), g(0x40), g(0x41), g(0x42), g(0x43), g(0x44), g(0x45), g(0x46), g(0x47),
                     (g(0x48) << 8) | g(0x49), (g(0x4A) << 8) | g(0x4B), g(0x4D), g(0x4E), g(0x4F)])
    return rows


class WbChannel(threading.Thread):
    """One AEM serial input.  Keeps the newest AFR value and its time."""

    def __init__(self, idx, port, serial_factory=None):
        super().__init__(daemon=True)
        self.idx = idx
        self.port = port
        self.afr = None
        self.t = 0.0
        self.notify = None             # threading.Event set by the relay: wakes the sender on every new reading
        self.state = "starting"        # starting / CONNECTED / ERROR: text
        self.stop_evt = threading.Event()
        self._factory = serial_factory

    def run(self):
        try:
            if self._factory:
                conn = self._factory(self.port)
            else:
                conn = serial.Serial(port=self.port, baudrate=9600, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                                     stopbits=serial.STOPBITS_ONE, rtscts=True, timeout=0.2)
            self.state = "CONNECTED"
            buf = b""
            while not self.stop_evt.is_set():
                data = conn.read(256)
                if data:
                    buf += data
                    if len(buf) > 2048:
                        buf = buf[-256:]               # junk without line ends: keep only the tail
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        v = wb_parse_afr(line.decode("ascii", errors="ignore"))
                        if v is not None:
                            self.afr = v
                            self.t = time.monotonic()
                            if self.notify is not None:
                                self.notify.set()
                elif not self._factory:
                    time.sleep(0.01)
            try:
                conn.close()
            except Exception:
                pass
        except Exception as err:
            self.state = f"ERROR: {err}"


class WbRelay:
    """Owns the two channels and the sender thread.  send(bytes) must be thread safe (the viewer passes a locked port B write)."""

    def __init__(self, send, ports, stoich=WB_STOICH, serial_factory=None):
        self.send = send
        self.stoich = stoich
        self.chans = [WbChannel(i + 1, p, serial_factory) for i, p in enumerate(ports) if p]
        self.stop_evt = threading.Event()
        self.thread = None
        self.history = [[], []]                        # (t, afr) per channel for the graph
        self.on_row = None                             # callback(t, afr1, afr2) for the wideband log file
        self.sent = 0
        self._last_hb = 0.0
        self.wake = threading.Event()
        for c in self.chans:
            c.notify = self.wake

    def start(self):
        self.send(b"WB 1\n")
        for c in self.chans:
            c.start()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        min_gap = 1.0 / WB_SEND_HZ
        last = 0.0
        seen = [0.0, 0.0]
        while not self.stop_evt.is_set():
            self.wake.wait(WB_HEARTBEAT_S)             # a new AEM reading wakes us at once
            self.wake.clear()
            if self.stop_evt.is_set():
                return
            now = time.monotonic()
            gap = min_gap - (now - last)
            if gap > 0:
                self.stop_evt.wait(gap)
                now = time.monotonic()
            last = now
            vals = [None, None]
            fresh = False
            for c in self.chans:
                if c.afr is not None and now - c.t < WB_STALE_S:
                    vals[c.idx - 1] = c.afr
                    if c.t != seen[c.idx - 1]:         # log/graph only real new readings, not repeats
                        seen[c.idx - 1] = c.t
                        fresh = True
                        h = self.history[c.idx - 1]
                        h.append((now, c.afr))
                        if len(h) > 1200:
                            del h[:200]
            try:
                # v0.3.13 test: ONE write for both channels, and nothing when neither value changed since the last send
                # (except the heartbeat).  Fewer small USB packets to port B (the crash only happened with AEM on).
                msg = "".join(f"L{i + 1} {wb_lambda_milli(vals[i], self.stoich)}\n" for i in (0, 1) if vals[i] is not None)
                if msg and (fresh or now - self._last_hb >= WB_HEARTBEAT_S):
                    self.send(msg.encode())
                    self._last_hb = now
                    self.sent += 1
            except Exception:
                return                                    # port B gone: the viewer's own error handling reports it
            if fresh and self.on_row:
                try:
                    self.on_row(now, vals[0], vals[1])
                except Exception:
                    pass

    def stop(self, send_off=True):
        self.stop_evt.set()
        self.wake.set()
        for c in self.chans:
            c.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=1.0)
        if send_off:
            try:
                self.send(b"WB 0\n")
            except Exception:
                pass


class EPROMHexViewer(tk.Tk):
    CONFIG_FILE = os.path.join(BASE_DIR, "eprom_viewer_config.json")

    def __init__(self):
        super().__init__()

        self.title("27C512 (64KB) EPROM Behaviour Logger — port B recorder, live hits, XDF map layer")
        self.geometry("1280x780")
        self.minsize(1024, 600)

        # 27C512 Capacity: 64 KB = 65,536 bytes
        self.EPROM_SIZE = 65536
        self.bin_data = bytearray([0xFF] * self.EPROM_SIZE)
        
        # Overlay Data Structures
        self.accessed_addresses = set()  # Log file overlay addresses
        self.xdf_maps = []               # List of dicts: {title, address, size, rows, cols, color, type}
        
        # Default Log Layer & Dot Marker Settings
        self.log_highlight_color = "#00E676"  # Highlight color for Log overlay text
        self.log_dot_color = "#00E676"        # Dot color
        self.log_dot_size = 11                # Dot font size / scale
        self.log_dot_symbol = "●"             # Dot marker shape: ●, •, 🔴, 🟢, 🟡, 🔵, ■
        self.log_cover_text = True            # Cover / mask text underneath dot
        
        # Live logger (port B)
        self.port_name = ""                 # last used COM port (saved)
        self.live_window = 10               # seconds of history shown on the BIN (0 = everything recorded)
        self.live_enabled = True
        self._cell_xdf = {}                 # byte address -> xdf tag name (for cell-wise redraw)
        self.q = queue.Queue()
        self.ser = None
        self.ser_lock = threading.Lock()    # the window thread, the reader thread and the wideband sender all write to port B
        self.reader = None
        self.wb_relay = None                # running only while the wideband function is enabled
        self.wb_win = None
        self.wb_file = None
        self.wb_enabled = False
        self.wb_ports = ["COM20", "COM21"]
        self.wb_show_lambda = False
        self.wb_stoich = WB_STOICH
        self.wb_state = "off"
        self.rpm_text = ""
        self.rx_frames = 0                  # address frames received (reader thread counts, _poll reads)
        self.last_hb_t = 0.0                # when the device last sent its once-a-second health frame
        self.rate_t = 0.0
        self.rate_n = 0
        self.rate_base = 0.0                # typical frames per second (slow average)
        self.rate_low = 0                   # consecutive low-rate seconds
        self.rate_alarm = False
        self.link_alarm = False
        self.err_count = 0
        self.last_drop = 0
        self.stop_evt = threading.Event()
        self.rec_file = None
        self.rec_lock = threading.Lock()    # the reader thread writes the log file, the window thread starts/stops it
        self.rec_seq = 0
        self.rec_stats = {}                 # address -> [hits, first_s, last_s] of this recording (for the summary file)
        self.rec_flush_t = 0.0
        self.rec_path = None
        self.last_rec_path = None
        self.last_seen = {}                 # address -> host time (monotonic) of the last hit
        self.hit_count = {}                 # address -> hits in this recording
        self.rec_t0 = 0.0
        self.rec_lines = 0
        self.frames = 0
        self.dev_drop = 0
        self.dev_over = 0
        self.connected = False
        self.recording = False
        self.paused = False
        self.hello = False
        self.shown = set()                  # addresses currently drawn as hit

        # Persistent File Locations
        self.last_bin_path = None
        self.last_xdf_path = None
        self.last_log_path = None
        self.rpm_k = 0                      # RPM calibration constant of the One ROM (restored on every Connect)
        self.rpm_real = 2100                # last real RPM typed in the calibration window
        self.win_geometry = ""

        # Load persistent dot settings and last file locations if config exists
        self.load_settings()

        # Selection & State Tracking
        self.selected_address = None          # Currently clicked byte address
        
        # Color palette for auto-assigning to XDF map layers
        self.map_color_palette = [
            "#264F78", "#1B4D3E", "#4A2E35", "#3D3B1D", 
            "#4A154B", "#004D40", "#311B92", "#827717", 
            "#BF360C", "#3E2723", "#0D47A1", "#1B5E20"
        ]

        self._configure_styles()
        self._build_ui()
        self._build_context_menu()
        self._render_hex_view()
        
        # Auto-load saved BIN and XDF files on application boot
        self._auto_load_last_files()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.win_geometry:
            try:
                self.geometry(self.win_geometry)
            except Exception:
                pass
        self.after(30000, self._autosave_tick)
        self.report_callback_exception = self._report_exception
        self.after(200, self._poll)
        self._blink_on = False
        self.after(600, self._blink_rec)

    def _report_exception(self, exc, val, tb):
        """Any error inside a button/timer is shown and written to eprom_viewer_error.log instead of silently killing things."""
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(ERR_LOG, "a", encoding="utf-8") as f:
                f.write(datetime.datetime.now().isoformat() + "\n" + text + "\n")
        except Exception:
            pass
        try:
            self.status_bar.config(text=f" ERROR: {val}  (details in {ERR_LOG})")
        except Exception:
            pass
        print(text)

    # --- Persistent Configuration Loading & Saving ---
    def load_settings(self):
        """Loads saved dot settings and last BIN/XDF file paths from JSON config file."""
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    self.log_dot_color = cfg.get("log_dot_color", self.log_dot_color)
                    self.log_highlight_color = self.log_dot_color
                    self.log_dot_size = cfg.get("log_dot_size", self.log_dot_size)
                    self.log_dot_symbol = cfg.get("log_dot_symbol", self.log_dot_symbol)
                    self.log_cover_text = cfg.get("log_cover_text", self.log_cover_text)
                    self.last_bin_path = cfg.get("last_bin_path", self.last_bin_path)
                    self.last_xdf_path = cfg.get("last_xdf_path", self.last_xdf_path)
                    self.last_log_path = cfg.get("last_log_path", self.last_log_path)
                    self.rpm_k = int(cfg.get("rpm_k", self.rpm_k))
                    self.rpm_real = int(cfg.get("rpm_real", self.rpm_real))
                    self.win_geometry = str(cfg.get("win_geometry", self.win_geometry))
                    self.port_name = cfg.get("port_name", self.port_name)
                    self.live_window = cfg.get("live_window", self.live_window)
                    self.wb_enabled = bool(cfg.get("wb_enabled", self.wb_enabled))
                    self.wb_ports = list(cfg.get("wb_ports", self.wb_ports))[:2]
                    self.wb_show_lambda = bool(cfg.get("wb_show_lambda", self.wb_show_lambda))
                    self.wb_stoich = float(cfg.get("wb_stoich", self.wb_stoich))
            except Exception as e:
                print(f"Failed to load settings file: {e}")

    def save_settings(self):
        """Saves dot settings and current file locations persistently to JSON config file."""
        cfg = {
            "log_dot_color": self.log_dot_color,
            "log_dot_size": self.log_dot_size,
            "log_dot_symbol": self.log_dot_symbol,
            "log_cover_text": self.log_cover_text,
            "last_bin_path": self.last_bin_path,
            "last_xdf_path": self.last_xdf_path,
            "last_log_path": self.last_log_path,
            "rpm_k": self.rpm_k,
            "rpm_real": self.rpm_real,
            "win_geometry": self.win_geometry,
            "port_name": self.port_name,
            "live_window": self.live_window,
            "wb_enabled": self.wb_enabled,
            "wb_ports": self.wb_ports,
            "wb_show_lambda": self.wb_show_lambda,
            "wb_stoich": self.wb_stoich
        }
        try:
            tmp = self.CONFIG_FILE + ".tmp"                 # write a temp file, then swap: a crash never leaves a half-written config
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4)
            os.replace(tmp, self.CONFIG_FILE)
        except Exception as e:
            print(f"Failed to save settings: {e}")

    def _auto_load_last_files(self):
        """Auto-loads BIN and XDF files on startup if previously saved paths exist on disk."""
        restored = []
        if self.last_bin_path and os.path.exists(self.last_bin_path):
            if self.load_bin_file(file_path=self.last_bin_path, auto_boot=True):
                restored.append(f"BIN: {os.path.basename(self.last_bin_path)}")
                
        if self.last_xdf_path and os.path.exists(self.last_xdf_path):
            if self.load_xdf_file(file_path=self.last_xdf_path, auto_boot=True):
                restored.append(f"XDF: {os.path.basename(self.last_xdf_path)}")

        if self.last_log_path and os.path.exists(self.last_log_path):
            try:
                self.load_log_file(file_path=self.last_log_path, auto_boot=True)
                restored.append(f"LOG: {os.path.basename(self.last_log_path)}")
            except Exception:
                pass

        if restored:
            self.status_bar.config(text=f" 🔄 Auto-restored session files: {', '.join(restored)}")

    def _configure_styles(self):
        self.configure(bg="#1E1E1E")
        self.style = ttk.Style(self)
        self.style.theme_use("clam")

        self.style.configure(".", background="#1E1E1E", foreground="#CCCCCC", font=("Segoe UI", 10))
        self.style.configure("TFrame", background="#1E1E1E")
        self.style.configure("TLabel", background="#1E1E1E", foreground="#CCCCCC")
        self.style.configure("TButton", background="#333333", foreground="#FFFFFF", borderwidth=1)
        self.style.map("TButton", background=[("active", "#444444")])
        # Record button while recording: red, slowly blinking between bright and dark red
        self.style.configure("RecOn.TButton", background="#E53935", foreground="#FFFFFF")
        self.style.map("RecOn.TButton", background=[("active", "#E53935")])
        self.style.configure("RecDim.TButton", background="#6B1B1B", foreground="#FFFFFF")
        self.style.map("RecDim.TButton", background=[("active", "#6B1B1B")])
        
        # Sidebar Map Treeview Dark Styling
        self.style.configure(
            "Treeview",
            background="#252526",
            foreground="#CCCCCC",
            fieldbackground="#252526",
            rowheight=22,
            font=("Segoe UI", 9)
        )
        self.style.configure("Treeview.Heading", background="#333333", foreground="#FFFFFF", font=("Segoe UI", 9, "bold"))
        self.style.map("Treeview", background=[("selected", "#007ACC")], foreground=[("selected", "#FFFFFF")])

    def _build_ui(self):
        # 1. Top Toolbar
        toolbar = ttk.Frame(self, padding=6)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="📂 Load BIN", command=self.load_bin_file).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="🗺️ Load XDF Layer", command=self.load_xdf_file).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="⚙️ Dot Settings", command=self.show_dot_settings_dialog).pack(side=tk.LEFT, padx=2)

        # Copy Address Quick Button
        ttk.Button(toolbar, text="📋 Copy Addr", command=self.copy_selected_address).pack(side=tk.LEFT, padx=(8, 2))

        # Address Jump Box
        ttk.Label(toolbar, text="  Go to (Hex):").pack(side=tk.LEFT, padx=(8, 2))
        self.entry_goto = ttk.Entry(toolbar, width=8)
        self.entry_goto.pack(side=tk.LEFT, padx=2)
        self.entry_goto.bind("<Return>", lambda e: self.goto_address())
        ttk.Button(toolbar, text="Go", width=4, command=self.goto_address).pack(side=tk.LEFT, padx=2)

        # AEM wideband / RPM calibration live in the top row (free space), link status at the right end
        self.btn_wb = ttk.Button(toolbar, text="AEM Wideband...", command=self.show_wideband_dialog)
        self.btn_wb.pack(side=tk.LEFT, padx=(14, 2))
        ttk.Button(toolbar, text="RPM calibrate...", command=self.show_rpm_dialog).pack(side=tk.LEFT, padx=2)
        self.lbl_link = tk.Label(toolbar, text="  \u25cf port B: not connected  ", fg="#ffffff", bg="#707070", font=("Segoe UI", 9, "bold"))
        self.lbl_link.pack(side=tk.RIGHT, padx=4)

        # 1b. Second row: log file + port B logger
        tb2 = ttk.Frame(self, padding=(6, 0, 6, 4))
        tb2.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(tb2, text="📑 Load Log", command=self.load_log_file).pack(side=tk.LEFT, padx=2)
        ttk.Button(tb2, text="TunerPro .xdl -> CSV", command=self.convert_xdl).pack(side=tk.LEFT, padx=2)
        ttk.Button(tb2, text="💾 Save Log", command=self.save_log_as).pack(side=tk.LEFT, padx=2)
        ttk.Button(tb2, text="🗑️ Clear", command=self.clear_log_overlay).pack(side=tk.LEFT, padx=2)
        ttk.Label(tb2, text="  Port B:").pack(side=tk.LEFT, padx=(10, 2))
        self.combo_port = ttk.Combobox(tb2, width=30, state="readonly")
        self.combo_port.pack(side=tk.LEFT, padx=2)
        ttk.Button(tb2, text="↻", width=3, command=self.refresh_ports).pack(side=tk.LEFT, padx=2)
        self.btn_connect = ttk.Button(tb2, text="🔌 Connect", command=self.toggle_connect)
        self.btn_connect.pack(side=tk.LEFT, padx=(6, 2))
        self.btn_record = ttk.Button(tb2, text="⏺ Record", command=self.start_record)
        self.btn_record.pack(side=tk.LEFT, padx=2)
        self.btn_stop = ttk.Button(tb2, text="⏹ Stop", command=self.stop_record)
        self.btn_stop.pack(side=tk.LEFT, padx=2)
        self.btn_pause = ttk.Button(tb2, text="⏸ Pause", command=self.toggle_pause)
        self.btn_pause.pack(side=tk.LEFT, padx=2)
        ttk.Label(tb2, text="  Show last (s, 0=all):").pack(side=tk.LEFT, padx=(10, 2))
        self.spin_window = ttk.Spinbox(tb2, from_=0, to=3600, width=5, command=self._window_changed)
        self.spin_window.set(self.live_window)
        self.spin_window.pack(side=tk.LEFT, padx=2)
        self.spin_window.bind("<KeyRelease>", lambda e: self._window_changed())
        self.var_live = tk.BooleanVar(value=True)
        ttk.Checkbutton(tb2, text="Live view", variable=self.var_live, command=self._window_changed).pack(side=tk.LEFT, padx=6)
        self.lbl_live = ttk.Label(tb2, text="not connected")
        self.lbl_live.pack(side=tk.LEFT, padx=8)
        self.refresh_ports()

        # 2. Main Split Area (Left Sidebar: XDF Map List | Right: Hex Viewer)
        main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=4)

        # --- Left Panel: XDF Map Table ---
        left_frame = ttk.Frame(main_pane, padding=2)
        main_pane.add(left_frame, weight=1)

        ttk.Label(left_frame, text="XDF Defined Maps / Tables", font=("Segoe UI", 10, "bold")).pack(side=tk.TOP, anchor="w", pady=(0, 4))

        tree_scroll = ttk.Scrollbar(left_frame, orient=tk.VERTICAL)
        self.map_tree = ttk.Treeview(
            left_frame, 
            columns=("Addr", "Size", "Dim", "Title"), 
            show="headings", 
            yscrollcommand=tree_scroll.set
        )
        tree_scroll.config(command=self.map_tree.yview)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.map_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.map_tree.heading("Addr", text="Addr")
        self.map_tree.heading("Size", text="Bytes")
        self.map_tree.heading("Dim", text="Dim")
        self.map_tree.heading("Title", text="Map Name")

        self.map_tree.column("Addr", width=65, anchor="center")
        self.map_tree.column("Size", width=50, anchor="center")
        self.map_tree.column("Dim", width=55, anchor="center")
        self.map_tree.column("Title", width=180, anchor="w")

        self.map_tree.bind("<<TreeviewSelect>>", self.on_map_selected)

        # --- Right Panel: Hex Viewer Display ---
        right_frame = ttk.Frame(main_pane, padding=2)
        main_pane.add(right_frame, weight=3)

        header_text = "Offset    00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F   ASCII             MAP INFO AT SELECTED LINE"
        lbl_header = tk.Label(
            right_frame, 
            text=header_text, 
            font=("Consolas", 11, "bold"), 
            bg="#252526", 
            fg="#569CD6", 
            anchor="w"
        )
        lbl_header.pack(fill=tk.X)

        viewer_inner = ttk.Frame(right_frame)
        viewer_inner.pack(fill=tk.BOTH, expand=True)

        self.hex_text = tk.Text(
            viewer_inner,
            wrap=tk.NONE,
            font=("Consolas", 11),
            bg="#1E1E1E",
            fg="#D4D4D4",
            insertbackground="white",
            selectbackground="#007ACC",
            cursor="hand2"
        )
        self.hex_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(viewer_inner, orient=tk.VERTICAL, command=self.hex_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.hex_text.configure(yscrollcommand=scrollbar.set)

        self.hex_text.tag_configure("addr_col", foreground="#858585")
        self.hex_text.tag_configure("ascii_col", foreground="#CE9178")

        # Cell Selection & Keyboard Shortcuts
        self.hex_text.bind("<Button-1>", self.on_hex_click)
        self.hex_text.bind("<Button-3>", self.on_hex_right_click)
        self.hex_text.bind("<Motion>", self.on_hex_hover)
        self.bind_all("<Control-c>", lambda e: self.copy_selected_address())

        # 3. Bottom Status Bar
        self.status_bar = ttk.Label(
            self, 
            text=" Status: Blank 64KB EPROM space ready. Click cell to select & copy address.", 
            relief=tk.SUNKEN, 
            anchor="w",
            padding=4
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _build_context_menu(self):
        """Builds right-click context menu for cell copy operations."""
        self.context_menu = tk.Menu(self, tearoff=0, bg="#2D2D2D", fg="#FFFFFF", activebackground="#007ACC")
        self.context_menu.add_command(label="📋 Copy Address (0x0000)", command=self.copy_selected_address)
        self.context_menu.add_command(label="📋 Copy Hex Address Only", command=lambda: self.copy_selected_address(format_type="hex_only"))
        self.context_menu.add_command(label="📋 Copy Decimal Address", command=lambda: self.copy_selected_address(format_type="dec"))
        self.context_menu.add_command(label="🔢 Copy Byte Value", command=self.copy_selected_byte_value)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🗺️ Select Belonging Map", command=self.jump_to_selected_cell_map)

    def _render_hex_view(self):
        """Renders binary buffer into formatted hex/ascii rows with optional Dot Masking."""
        self.hex_text.config(state=tk.NORMAL)
        self.hex_text.delete("1.0", tk.END)

        lines = []
        sym = self.log_dot_symbol

        for offset in range(0, self.EPROM_SIZE, 16):
            chunk = self.bin_data[offset:offset + 16]
            
            # Build Hex tokens (Columns 00-07)
            hex_tokens_1 = []
            for i in range(8):
                addr = offset + i
                if addr in self.accessed_addresses and self.log_cover_text:
                    hex_tokens_1.append(f"{sym} ")
                else:
                    hex_tokens_1.append(f"{chunk[i]:02X}")

            # Build Hex tokens (Columns 08-0F)
            hex_tokens_2 = []
            for i in range(8, 16):
                addr = offset + i
                if addr in self.accessed_addresses and self.log_cover_text:
                    hex_tokens_2.append(f"{sym} ")
                else:
                    hex_tokens_2.append(f"{chunk[i]:02X}")

            # Build ASCII representation
            ascii_chars = []
            for i in range(16):
                addr = offset + i
                if addr in self.accessed_addresses and self.log_cover_text:
                    ascii_chars.append(f"{sym}")
                else:
                    b = chunk[i]
                    ascii_chars.append(chr(b) if 32 <= b <= 126 else ".")

            line_str = f"{offset:04X}      {' '.join(hex_tokens_1)}  {' '.join(hex_tokens_2)}   |{''.join(ascii_chars)}|"
            lines.append(line_str)

        self.hex_text.insert("1.0", "\n".join(lines))

        # Styling column defaults
        for line_num in range(1, (self.EPROM_SIZE // 16) + 1):
            self.hex_text.tag_add("addr_col", f"{line_num}.0", f"{line_num}.4")
            self.hex_text.tag_add("ascii_col", f"{line_num}.62", f"{line_num}.78")

        self.hex_text.config(state=tk.DISABLED)

        # Apply XDF map colors, dot overlays, and cell selection highlights
        self._apply_xdf_highlights()
        self._apply_log_highlights()
        if self.selected_address is not None:
            self._highlight_selected_cell(self.selected_address)

    # --- BIN Loading & Auto Save ---
    def load_bin_file(self, file_path=None, auto_boot=False):
        """Loads EPROM BIN file and updates auto-saved file location in config."""
        if file_path is None:
            file_path = filedialog.askopenfilename(
                title="Open EPROM BIN File",
                filetypes=[("Binary Files", "*.bin *.rom *.hex *.ori"), ("All Files", "*.*")]
            )

        if not file_path or not os.path.exists(file_path):
            return False

        try:
            with open(file_path, "rb") as f:
                data = f.read()

            self.bin_data = bytearray([0xFF] * self.EPROM_SIZE)
            copy_len = min(len(data), self.EPROM_SIZE)
            self.bin_data[:copy_len] = data[:copy_len]

            self.last_bin_path = file_path
            self.save_settings()

            self._render_hex_view()
            if not auto_boot:
                self.status_bar.config(text=f" Status: Loaded BIN file '{os.path.basename(file_path)}' ({copy_len} bytes) & saved location.")
            return True
        except Exception as err:
            if not auto_boot:
                messagebox.showerror("Error", f"Failed to read BIN file:\n{err}")
            return False

    # --- XDF Parsing Layer & Auto Save ---
    def load_xdf_file(self, file_path=None, auto_boot=False):
        """Parses XDF map definition file and saves location to config."""
        if file_path is None:
            file_path = filedialog.askopenfilename(
                title="Open XDF Map Specification",
                filetypes=[("XDF / XML Files", "*.xdf *.xml"), ("All Files", "*.*")]
            )

        if not file_path or not os.path.exists(file_path):
            return False

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except Exception as err:
            if not auto_boot:
                messagebox.showerror("XDF Parse Error", f"Failed to parse XDF file:\n{err}")
            return False

        self.xdf_maps.clear()
        palette_idx = 0

        # Parse XDF Table definitions
        for table in root.findall(".//XDFTABLE"):
            title_elem = table.find("title")
            title = title_elem.text.strip() if title_elem is not None and title_elem.text else "Unnamed Table"
            
            z_axis = table.find("XDFAXIS[@id='z']")
            emb = z_axis.find("EMBEDDEDDATA") if z_axis is not None else None
            
            if emb is None:
                for axis in table.findall("XDFAXIS"):
                    e = axis.find("EMBEDDEDDATA")
                    if e is not None and "mmedaddress" in e.attrib:
                        emb = e
                        break

            if emb is not None and "mmedaddress" in emb.attrib:
                addr_raw = emb.attrib["mmedaddress"]
                try:
                    addr = int(addr_raw, 16) if addr_raw.lower().startswith("0x") else int(addr_raw)
                except ValueError:
                    continue

                rows = int(emb.attrib.get("mmedrowcount", 1))
                cols = int(emb.attrib.get("mmedcolcount", 1))
                bits = int(emb.attrib.get("mmedelementsizebits", 8))
                bytes_per_elem = max(1, bits // 8)
                size_bytes = rows * cols * bytes_per_elem
                color = self.map_color_palette[palette_idx % len(self.map_color_palette)]
                palette_idx += 1

                self.xdf_maps.append({
                    "title": title,
                    "address": addr,
                    "size": size_bytes,
                    "rows": rows,
                    "cols": cols,
                    "color": color,
                    "type": "Table"
                })

        # Parse XDF Constants
        for const in root.findall(".//XDFCONSTANT"):
            title_elem = const.find("title")
            title = title_elem.text.strip() if title_elem is not None and title_elem.text else "Unnamed Constant"
            emb = const.find("EMBEDDEDDATA")
            if emb is not None and "mmedaddress" in emb.attrib:
                addr_raw = emb.attrib["mmedaddress"]
                try:
                    addr = int(addr_raw, 16) if addr_raw.lower().startswith("0x") else int(addr_raw)
                except ValueError:
                    continue

                bits = int(emb.attrib.get("mmedelementsizebits", 8))
                size_bytes = max(1, bits // 8)
                color = self.map_color_palette[palette_idx % len(self.map_color_palette)]
                palette_idx += 1

                self.xdf_maps.append({
                    "title": title,
                    "address": addr,
                    "size": size_bytes,
                    "rows": 1,
                    "cols": 1,
                    "color": color,
                    "type": "Scalar"
                })

        self.xdf_maps.sort(key=lambda item: item["address"])
        self.last_xdf_path = file_path
        self.save_settings()

        self._populate_map_tree()
        self._apply_xdf_highlights()
        if not auto_boot:
            self.status_bar.config(text=f" Status: Loaded {len(self.xdf_maps)} maps from '{os.path.basename(file_path)}' & saved location.")
        return True

    # --- Cell Selection & Same-Line Map Info ---
    def on_hex_click(self, event):
        """Handles left click to select cell and display map info on the same line."""
        addr = self._get_address_from_event(event)
        if addr is not None:
            self.selected_address = addr
            self._highlight_selected_cell(addr)
            self._update_same_line_map_info(addr)
            val = self.bin_data[addr]
            self.status_bar.config(
                text=f" 🎯 Selected Cell: Addr 0x{addr:04X} ({addr}) | Val: 0x{val:02X} ({val}) | Press '📋 Copy Addr' or Ctrl+C to copy"
            )

    def on_hex_right_click(self, event):
        """Handles right click to select cell and open context menu."""
        addr = self._get_address_from_event(event)
        if addr is not None:
            self.selected_address = addr
            self._highlight_selected_cell(addr)
            self._update_same_line_map_info(addr)
            
            # Update menu label dynamically with address
            self.context_menu.entryconfigure(0, label=f"📋 Copy Address (0x{addr:04X})")
            self.context_menu.post(event.x_root, event.y_root)

    def _get_address_from_event(self, event):
        """Converts text widget click coordinates into EPROM byte address."""
        index = self.hex_text.index(f"@{event.x},{event.y}")
        line_str, col_str = index.split(".")
        line_num, col_num = int(line_str), int(col_str)

        row_idx = line_num - 1
        byte_in_row = self._get_byte_col_from_char_col(col_num)

        if byte_in_row is not None and 0 <= byte_in_row < 16:
            addr = (row_idx * 16) + byte_in_row
            if addr < self.EPROM_SIZE:
                return addr
        return None

    def _get_byte_col_from_char_col(self, col_num):
        """Maps character column position to byte index (0-15)."""
        if 10 <= col_num <= 32:
            return (col_num - 10) // 3
        elif 35 <= col_num <= 57:
            return 8 + (col_num - 35) // 3
        elif 62 <= col_num <= 77:
            return col_num - 62
        return None

    def _highlight_selected_cell(self, addr):
        """Highlights selected cell with a button-style high-contrast box."""
        self.hex_text.config(state=tk.NORMAL)
        self.hex_text.tag_remove("selected_cell", "1.0", tk.END)

        line_num = (addr // 16) + 1
        col_in_row = addr % 16
        hs, he, as_, ae = self._get_char_indices_for_byte(col_in_row)

        self.hex_text.tag_configure(
            "selected_cell", 
            background="#007ACC", 
            foreground="#FFFFFF", 
            font=("Consolas", 11, "bold"),
            relief=tk.RAISED
        )

        self.hex_text.tag_add("selected_cell", f"{line_num}.{hs}", f"{line_num}.{he}")
        self.hex_text.tag_add("selected_cell", f"{line_num}.{as_}", f"{line_num}.{ae}")
        self.hex_text.tag_raise("selected_cell")

        self.hex_text.config(state=tk.DISABLED)

    def _update_same_line_map_info(self, addr):
        """Displays map belonging information on the right side of the SAME line where clicked."""
        self.hex_text.config(state=tk.NORMAL)
        
        # Remove previous same-line map info annotations
        for tag in self.hex_text.tag_names():
            if tag.startswith("map_line_info_"):
                self.hex_text.tag_delete(tag)
        
        # Delete previous inline map info texts
        for line_num in range(1, (self.EPROM_SIZE // 16) + 1):
            line_end = f"{line_num}.79"
            line_content = self.hex_text.get(line_end, f"{line_num}.end")
            if "◄" in line_content:
                self.hex_text.delete(line_end, f"{line_num}.end")

        # Find matching maps
        matching_maps = [
            m for m in self.xdf_maps 
            if m["address"] <= addr < (m["address"] + m["size"])
        ]

        target_line = (addr // 16) + 1
        tag_name = f"map_line_info_{target_line}"

        if matching_maps:
            map_titles = ", ".join(f"'{m['title']}'" for m in matching_maps)
            primary = matching_maps[0]
            info_str = f"  ◄ Map: {map_titles} [Addr: 0x{primary['address']:04X}, Size: {primary['size']}B ({primary['rows']}x{primary['cols']})]"
            self.hex_text.tag_configure(tag_name, background="#3A3A00", foreground="#FFD700", font=("Segoe UI", 9, "bold"))
        else:
            info_str = f"  ◄ Unmapped Area [Addr: 0x{addr:04X}]"
            self.hex_text.tag_configure(tag_name, background="#2A2A2A", foreground="#888888", font=("Segoe UI", 9, "italic"))

        # Insert info at same line (column 79)
        self.hex_text.insert(f"{target_line}.79", info_str)
        self.hex_text.tag_add(tag_name, f"{target_line}.79", f"{target_line}.end")

        self.hex_text.config(state=tk.DISABLED)

    # --- Address Copy & Actions ---
    def copy_selected_address(self, format_type="hex_prefix"):
        """Copies currently selected cell address to clipboard."""
        if self.selected_address is None:
            messagebox.showinfo("Select Cell", "Please click on a hex/dot cell first to select its address.")
            return

        addr = self.selected_address
        if format_type == "hex_prefix":
            text_to_copy = f"0x{addr:04X}"
        elif format_type == "hex_only":
            text_to_copy = f"{addr:04X}"
        elif format_type == "dec":
            text_to_copy = str(addr)

        self.clipboard_clear()
        self.clipboard_append(text_to_copy)
        self.status_bar.config(text=f" 📋 Copied address '{text_to_copy}' to system clipboard!")

    def copy_selected_byte_value(self):
        """Copies hex byte value of selected cell from binary memory."""
        if self.selected_address is None:
            return
        val = self.bin_data[self.selected_address]
        val_str = f"0x{val:02X}"
        self.clipboard_clear()
        self.clipboard_append(val_str)
        self.status_bar.config(text=f" 📋 Copied byte value '{val_str}' ({val}) to clipboard!")

    def jump_to_selected_cell_map(self):
        """Highlights the map in treeview corresponding to selected cell."""
        if self.selected_address is None:
            return
        addr = self.selected_address
        for idx, m in enumerate(self.xdf_maps):
            if m["address"] <= addr < (m["address"] + m["size"]):
                self.map_tree.selection_set(str(idx))
                self.map_tree.see(str(idx))
                self.status_bar.config(text=f" 🗺️ Selected Map: '{m['title']}' in sidebar.")
                return
        messagebox.showinfo("Unmapped", f"Address 0x{addr:04X} does not belong to any loaded XDF map.")

    def _populate_map_tree(self):
        """Populates sidebar tree with extracted XDF map entries."""
        for item in self.map_tree.get_children():
            self.map_tree.delete(item)

        for idx, m in enumerate(self.xdf_maps):
            dim_str = f"{m['rows']}x{m['cols']}"
            addr_hex = f"0x{m['address']:04X}"
            self.map_tree.insert(
                "", 
                tk.END, 
                iid=str(idx), 
                values=(addr_hex, m['size'], dim_str, m['title'])
            )

    def _apply_xdf_highlights(self):
        """Applies color highlights to hex view for every XDF map."""
        self._cell_xdf = {}
        if not self.xdf_maps:
            return

        self.hex_text.config(state=tk.NORMAL)
        
        for tag in self.hex_text.tag_names():
            if tag.startswith("xdf_tag_"):
                self.hex_text.tag_delete(tag)

        for idx, m in enumerate(self.xdf_maps):
            tag_name = f"xdf_tag_{idx}"
            self.hex_text.tag_configure(
                tag_name, 
                background=m["color"], 
                foreground="#FFFFFF"
            )

            for byte_addr in range(m["address"], m["address"] + m["size"]):
                if byte_addr >= self.EPROM_SIZE:
                    break

                self._cell_xdf[byte_addr] = tag_name
                line_num = (byte_addr // 16) + 1
                col_in_row = byte_addr % 16
                hs, he, as_, ae = self._get_char_indices_for_byte(col_in_row)

                self.hex_text.tag_add(tag_name, f"{line_num}.{hs}", f"{line_num}.{he}")
                self.hex_text.tag_add(tag_name, f"{line_num}.{as_}", f"{line_num}.{ae}")

        self.hex_text.config(state=tk.DISABLED)

    def _get_char_indices_for_byte(self, offset_in_row):
        """Maps byte offset (0-15) to text widget character column bounds."""
        if offset_in_row < 8:
            hex_start = 10 + offset_in_row * 3
        else:
            hex_start = 35 + (offset_in_row - 8) * 3
        hex_end = hex_start + 2

        ascii_start = 62 + offset_in_row
        ascii_end = ascii_start + 1
        return hex_start, hex_end, ascii_start, ascii_end

    # --- Interaction Events ---
    def on_map_selected(self, event):
        """Handles map selection from sidebar list and jumps to hex offset."""
        selected_items = self.map_tree.selection()
        if not selected_items:
            return

        idx = int(selected_items[0])
        m = self.xdf_maps[idx]

        line_num = (m["address"] // 16) + 1
        self.hex_text.see(f"{line_num}.0")
        if self.connected and self.ser and m.get("type") == "Table":       # tell the firmware which table's cursor to report
            try:
                self._ser_write(f"SEL {m['address']:X}\n".encode())
            except Exception:
                pass

        self.selected_address = m["address"]
        self._highlight_selected_cell(m["address"])
        self._update_same_line_map_info(m["address"])

        self.status_bar.config(
            text=f" 📍 Map Selected: '{m['title']}' | Addr: 0x{m['address']:04X} | Size: {m['size']} bytes ({m['rows']}x{m['cols']})"
        )

    def on_hex_hover(self, event):
        """Displays hover address and value in status bar."""
        addr = self._get_address_from_event(event)
        if addr is not None:
            val = self.bin_data[addr]
            matching_maps = [
                m for m in self.xdf_maps 
                if m["address"] <= addr < (m["address"] + m["size"])
            ]

            if matching_maps:
                map_info = " | ".join(f"Map: '{m['title']}'" for m in matching_maps)
                info_text = f" Hover Addr: 0x{addr:04X} | Val: 0x{val:02X} ({val}) | {map_info}"
            else:
                info_text = f" Hover Addr: 0x{addr:04X} | Val: 0x{val:02X} ({val}) | Unmapped Area"

            self.status_bar.config(text=info_text)

    # --- Log Overlay & Customizable DOT Layer ---
    def load_log_file(self, file_path=None, auto_boot=False):
        """Loads hit log file and applies DOT overlay."""
        if not file_path:
            file_path = filedialog.askopenfilename(
                title="Open Access Log File",
                filetypes=[("Log Files", "*.log *.txt *.csv"), ("All Files", "*.*")]
            )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            new_addrs = set()
            first = content.lstrip().split("\n", 1)[0].strip()
            if "addr" in first.lower().split(","):                 # a log recorded by this program (any version): find the addr column
                col = [c.strip().lower() for c in first.split(",")].index("addr")
                for line in content.splitlines()[1:]:
                    parts = line.split(",")
                    if len(parts) > col:
                        try:
                            val = int(parts[col], 16)
                        except ValueError:
                            continue
                        if val < self.EPROM_SIZE:
                            new_addrs.add(val)
            else:
                matches = re.findall(r"0x[0-9a-fA-F]+|[0-9a-fA-F]{4}", content)
                for m in matches:
                    val = int(m, 16)
                    if val < self.EPROM_SIZE:
                        new_addrs.add(val)

            self.accessed_addresses.update(new_addrs)
            self.shown = set(self.accessed_addresses)
            self._render_hex_view()
            self.last_log_path = file_path
            self.save_settings()
            self.status_bar.config(text=f" Status: Overlayed {len(new_addrs)} log hit addresses with DOT markers.")
        except Exception as err:
            if not auto_boot:
                messagebox.showerror("Error", f"Failed to load log file:\n{err}")

    def _apply_log_highlights(self):
        """Applies configured DOT markers and tags to log accessed addresses."""
        self.hex_text.config(state=tk.NORMAL)
        
        self.hex_text.tag_remove("log_hit", "1.0", tk.END)
        self.hex_text.tag_remove("log_dot", "1.0", tk.END)

        if not self.accessed_addresses:
            self.hex_text.config(state=tk.DISABLED)
            return

        self.hex_text.tag_configure("log_hit", foreground=self.log_highlight_color, font=("Consolas", 11, "bold"))
        self.hex_text.tag_configure("log_dot", foreground=self.log_dot_color, font=("Segoe UI", self.log_dot_size, "bold"))

        for addr in self.accessed_addresses:
            line_num = (addr // 16) + 1
            col_in_row = addr % 16
            hs, he, as_, ae = self._get_char_indices_for_byte(col_in_row)

            self.hex_text.tag_add("log_hit", f"{line_num}.{hs}", f"{line_num}.{he}")
            self.hex_text.tag_add("log_dot", f"{line_num}.{as_}", f"{line_num}.{ae}")

        self.hex_text.tag_raise("log_hit")
        self.hex_text.tag_raise("log_dot")
        if self.selected_address is not None:
            self.hex_text.tag_raise("selected_cell")

        self.hex_text.config(state=tk.DISABLED)

    def clear_log_overlay(self):
        """Clears log overlay markings."""
        self.accessed_addresses.clear()
        self.shown = set()
        self.last_seen.clear()
        self.hit_count.clear()
        self._render_hex_view()
        self.status_bar.config(text=" Status: Cleared log overlay markers.")

    def show_dot_settings_dialog(self):
        """Opens settings window to adjust and permanently save Dot settings."""
        dlg = tk.Toplevel(self)
        dlg.title("⚙️ Log Layer Dot Settings")
        dlg.geometry("400x340")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        frame = ttk.Frame(dlg, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # 1. Dot Color Selector
        ttk.Label(frame, text="Dot Color:", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=6)
        
        color_btn_var = tk.StringVar(value=self.log_dot_color)
        btn_color = tk.Button(
            frame, 
            text=" Choose Color ", 
            bg=self.log_dot_color, 
            fg="#000000",
            relief=tk.RAISED,
            command=lambda: self._choose_dot_color(btn_color, color_btn_var)
        )
        btn_color.grid(row=0, column=1, sticky="w", padx=10, pady=6)

        # 2. Dot Size Control
        ttk.Label(frame, text="Dot Size (pt):", font=("Segoe UI", 10, "bold")).grid(row=1, column=0, sticky="w", pady=6)
        spin_size = ttk.Spinbox(frame, from_=6, to=20, width=6)
        spin_size.set(self.log_dot_size)
        spin_size.grid(row=1, column=1, sticky="w", padx=10, pady=6)

        # 3. Dot Style Symbol
        ttk.Label(frame, text="Dot Symbol:", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, sticky="w", pady=6)
        symbols = ["● Circle", "• Bullet", "🔴 Red Circle", "🟢 Green Circle", "🟡 Yellow Circle", "🔵 Blue Circle", "■ Square"]
        combo_symbol = ttk.Combobox(frame, values=symbols, state="readonly", width=14)
        
        current_sym_idx = 0
        for i, s in enumerate(symbols):
            if s.startswith(self.log_dot_symbol):
                current_sym_idx = i
                break
        combo_symbol.current(current_sym_idx)
        combo_symbol.grid(row=2, column=1, sticky="w", padx=10, pady=6)

        # 4. Dot Always On Top (Covering / Masking Text)
        chk_cover_var = tk.BooleanVar(value=self.log_cover_text)
        chk_cover = ttk.Checkbutton(frame, text="Dot Always On Top (Mask / Cover Text)", variable=chk_cover_var)
        chk_cover.grid(row=3, column=0, columnspan=2, sticky="w", pady=6)

        # 5. Live Preview Box
        preview_frame = ttk.LabelFrame(frame, text=" Live Preview ", padding=8)
        preview_frame.grid(row=4, column=0, columnspan=2, fill=tk.X, pady=8)

        lbl_preview = tk.Label(
            preview_frame, 
            text=f"Log Hit Cell: [ {self.log_dot_symbol} ]", 
            bg="#1E1E1E", 
            fg=self.log_dot_color,
            font=("Consolas", self.log_dot_size, "bold")
        )
        lbl_preview.pack(fill=tk.X)

        def update_preview(*args):
            try:
                sz = int(spin_size.get())
            except ValueError:
                sz = 11
            clr = color_btn_var.get()
            sym = combo_symbol.get().split()[0]
            lbl_preview.config(text=f"Log Hit Cell: [ {sym} ]", fg=clr, font=("Consolas", sz, "bold"))

        spin_size.bind("<KeyRelease>", update_preview)
        spin_size.bind("<ButtonRelease-1>", update_preview)
        combo_symbol.bind("<<ComboboxSelected>>", update_preview)

        # 6. Save & Apply
        def apply_settings():
            try:
                self.log_dot_size = int(spin_size.get())
            except ValueError:
                self.log_dot_size = 11

            self.log_dot_color = color_btn_var.get()
            self.log_highlight_color = self.log_dot_color
            self.log_dot_symbol = combo_symbol.get().split()[0]
            self.log_cover_text = chk_cover_var.get()

            # Save persistently to eprom_viewer_config.json
            self.save_settings()

            self._render_hex_view()
            self.status_bar.config(
                text=f" Status: Saved & Applied Dot Settings to {self.CONFIG_FILE} (Color: {self.log_dot_color}, Size: {self.log_dot_size}pt)"
            )
            dlg.destroy()

        ttk.Button(frame, text="✅ Apply & Save", command=apply_settings).grid(row=5, column=0, columnspan=2, pady=6)

    def _choose_dot_color(self, btn_widget, color_var):
        color = colorchooser.askcolor(title="Select Log Dot Color", initialcolor=color_var.get())[1]
        if color:
            color_var.set(color)
            btn_widget.config(bg=color)

    # --- Port B logger: connect, record, live view ---------------------------------------------------------------
    def convert_xdl(self):
        """Open a TunerPro .xdl data log and save it as a CSV next to it (RPMAX, MAFAX, lambda, CROW/CCOL ...)."""
        path = filedialog.askopenfilename(title="TunerPro log (.xdl)", filetypes=[("TunerPro log", "*.xdl"), ("All files", "*.*")])
        if not path:
            return
        try:
            rows = xdl_rows(open(path, "rb").read())
            if not rows:
                messagebox.showwarning("XDL", "No records found in this file.")
                return
            out = os.path.splitext(path)[0] + ".csv"
            with open(out, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(XDL_COLS)
                w.writerows(rows)
            lam = [r[11] for r in rows if r[11]]
            msg = "%d records, %.1f s\nSaved: %s" % (len(rows), rows[-1][1] / 1000.0, out)
            if lam:
                msg += "\nLambda 1: %.3f ... %.3f" % (min(lam) / 1000.0, max(lam) / 1000.0)
            messagebox.showinfo("XDL -> CSV", msg)
        except Exception as e:
            messagebox.showerror("XDL", "Could not read the file: %s" % e)

    def refresh_ports(self):
        items = []
        if serial is not None:
            for p in serial.tools.list_ports.comports():
                tag = "   <- One ROM" if (p.vid == 0x1209) else ""
                items.append(f"{p.device} - {p.description}{tag}")
        self.combo_port["values"] = items
        pick = None
        for it in items:                                   # last used port first, else the one named "EPROM trace"
            if self.port_name and it.startswith(self.port_name + " "):
                pick = it
        if pick is None:
            for it in items:
                if "trace" in it.lower() or "epromlog" in it.lower().replace(" ", ""):
                    pick = it
        if pick is None and items:
            pick = items[0]
        if pick:
            self.combo_port.set(pick)

    def _window_changed(self):
        try:
            self.live_window = max(0, int(self.spin_window.get()))
        except ValueError:
            return
        self.live_enabled = bool(self.var_live.get())
        self.save_settings()
        self._refresh_live(force=True)

    def toggle_connect(self):
        if self.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if serial is None:
            messagebox.showerror("pyserial missing", "Install it once:   py -m pip install pyserial")
            return
        sel = self.combo_port.get().split(" ")[0]
        if not sel:
            messagebox.showwarning("Port", "Select the COM port of 'EPROM trace log (on request)' (port B).")
            return
        try:
            self.ser = serial.Serial(sel, 115200, timeout=0.2, dsrdtr=False, rtscts=False)
            self.ser.dtr = True
            self.ser.rts = True
        except Exception as err:
            messagebox.showerror("Port", f"Could not open {sel}:\n{err}")
            self.ser = None
            return
        self.port_name = sel
        self.save_settings()
        self.hello = False
        self.paused = False
        self.btn_pause.config(text="⏸ Pause")
        self.rec_t0 = time.monotonic()
        self.frames = 0
        self.rx_frames = 0
        self.rate_t = time.monotonic()
        self.rate_n = 0
        self.rate_base = 0.0
        self.rate_low = 0
        self.rate_alarm = self.link_alarm = False
        self.last_hb_t = time.monotonic()
        self.last_drop = 0
        self.last_seen.clear()
        self.hit_count.clear()
        self.stop_evt.clear()
        self.connected = True
        self.reader = threading.Thread(target=self._reader_thread, args=(self.ser,), daemon=True)
        self.reader.start()
        self.btn_connect.config(text="🔌 Disconnect")
        if self.rpm_k > 0:
            try:
                self._ser_write(f"RK {self.rpm_k}\n".encode())      # restore the saved RPM calibration in the One ROM
            except Exception:
                pass
        if self.wb_enabled:
            self._wb_start()
        self.status_bar.config(text=f" Connected to {sel}, handshake sent (TRACE). Live view is on; press Record to write a log file.")

    def disconnect(self):
        self._wb_stop(send_off=True)
        self.stop_evt.set()
        if self.reader:
            self.reader.join(timeout=1.0)
        if self.ser:
            try:
                self._ser_write(b"STOP\n")      # device goes silent again (saves its time and USB bandwidth)
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.reader = None
        self.connected = False
        self.stop_record(quiet=True)
        self.btn_connect.config(text="🔌 Connect")
        self.status_bar.config(text=f" Disconnected. {self.rec_lines} hits saved in {self.last_rec_path}" if self.last_rec_path else " Disconnected.")

    # --- AEM wideband (only active while enabled) -------------------------------------------------------------------
    def _ser_write(self, data):
        with self.ser_lock:
            ser = self.ser
            if ser is None:
                raise OSError("port B is not open")
            ser.write(data)

    def _wb_start(self, ser_factory=None):
        if self.wb_relay or not self.connected:
            return
        self.wb_relay = WbRelay(self._ser_write, self.wb_ports, self.wb_stoich, ser_factory)
        self.wb_relay.on_row = self._wb_row
        try:
            self.wb_relay.start()
        except Exception as err:
            self.wb_relay = None
            self.wb_state = f"error: {err}"
            return
        self.wb_state = "waiting for the device (WB 1 sent)"

    def _wb_stop(self, send_off=True):
        r, self.wb_relay = self.wb_relay, None
        if r:
            r.stop(send_off=send_off)
        self._wb_close_file()
        self.wb_state = "off"

    def _wb_reply(self, what):
        if what == "ON":
            self.wb_state = "ON: lambda goes to TunerPro"
        elif what == "OFF":
            self.wb_state = "off"
        else:                                           # NONE: this firmware was built with OST_WB=0
            self.wb_enabled = False
            self._wb_stop(send_off=False)
            self.wb_state = "this firmware has no viewer relay (v0.4-0.4.4 read AEM on the DIO pin). Use v0.4.5-relay, or the DIO wire"
            self.save_settings()

    def set_wideband(self, on):
        self.wb_enabled = bool(on)
        self.save_settings()
        if on:
            if self.connected:
                self._wb_start()
            else:
                self.wb_state = "enabled - starts when you press Connect"
        else:
            self._wb_stop(send_off=True)

    def _wb_row(self, now, a1, a2):
        """Called by the sender thread: while Record is running, write the wideband values next to the address log."""
        if not self.recording or not self.rec_path:
            return
        with self.rec_lock:
            if self.wb_file is None:
                try:
                    self.wb_file = open(self.rec_path[:-4] + "_wideband.csv", "w", encoding="utf-8", newline="")
                    self.wb_file.write("host_s,afr1,lambda1,afr2,lambda2\n")
                except Exception:
                    self.wb_file = False
            if self.wb_file:
                def cell(a):
                    return ("", "") if a is None else (f"{a:.2f}", f"{a / self.wb_stoich:.3f}")
                c1, c2 = cell(a1), cell(a2)
                self.wb_file.write(f"{now - self.rec_t0:.3f},{c1[0]},{c1[1]},{c2[0]},{c2[1]}\n")

    def _wb_close_file(self):
        with self.rec_lock:
            f, self.wb_file = self.wb_file, None
        if f:
            try:
                f.close()
            except Exception:
                pass

    def show_wideband_dialog(self):
        if self.wb_win and self.wb_win.winfo_exists():
            self.wb_win.lift()
            return
        win = tk.Toplevel(self)
        self.wb_win = win
        win.title("AEM Wideband -> One ROM -> TunerPro")
        win.geometry("560x420")
        top = ttk.Frame(win, padding=8)
        top.pack(fill=tk.X)
        var_en = tk.BooleanVar(value=self.wb_enabled)
        ttk.Checkbutton(top, text="Enable wideband (only then the firmware uses RAM and time for it)", variable=var_en,
                        command=lambda: self.set_wideband(var_en.get())).grid(row=0, column=0, columnspan=4, sticky="w")
        ports = []
        if serial is not None:
            ports = [p.device for p in serial.tools.list_ports.comports()]
        combos, lbl_afr, lbl_lam, lbl_st = [], [], [], []
        for i in range(2):
            ttk.Label(top, text=f"Sensor {i + 1}:").grid(row=i + 1, column=0, sticky="w", pady=3)
            cb = ttk.Combobox(top, width=9, values=ports)
            cb.set(self.wb_ports[i] if i < len(self.wb_ports) else "")
            cb.grid(row=i + 1, column=1, padx=4)
            combos.append(cb)
            la = ttk.Label(top, text="--", width=9, font=("Consolas", 16, "bold"))
            la.grid(row=i + 1, column=2, padx=6)
            lbl_afr.append(la)
            ls = ttk.Label(top, text="", width=24)
            ls.grid(row=i + 1, column=3, sticky="w")
            lbl_st.append(ls)
        var_lam = tk.BooleanVar(value=self.wb_show_lambda)
        ttk.Checkbutton(top, text="Show lambda instead of AFR", variable=var_lam).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(top, text="Stoich AFR:").grid(row=3, column=2, sticky="e")
        ent_st = ttk.Entry(top, width=6)
        ent_st.insert(0, f"{self.wb_stoich:g}")
        ent_st.grid(row=3, column=3, sticky="w")
        lbl_state = ttk.Label(win, text="", padding=(8, 0))
        lbl_state.pack(fill=tk.X)
        ttk.Label(win, text="Port changes apply the next time the wideband is switched on. Keep the checkbox off when you do not need it.",
                  padding=(8, 0), foreground="#666").pack(fill=tk.X)
        cv = tk.Canvas(win, bg="#101418", height=200, highlightthickness=0)
        cv.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        def apply_opts():
            self.wb_ports = [c.get().strip() for c in combos]
            self.wb_show_lambda = bool(var_lam.get())
            try:
                v = float(ent_st.get())
                if 5.0 <= v <= 30.0:
                    self.wb_stoich = v
                    if self.wb_relay:
                        self.wb_relay.stoich = v
            except ValueError:
                pass
            self.save_settings()

        for w in combos:
            w.bind("<<ComboboxSelected>>", lambda e: apply_opts())
            w.bind("<FocusOut>", lambda e: apply_opts())
        ent_st.bind("<FocusOut>", lambda e: apply_opts())
        var_lam.trace_add("write", lambda *a: apply_opts())
        win.protocol("WM_DELETE_WINDOW", lambda: (apply_opts(), win.destroy()))

        def fmt(afr):
            if afr is None:
                return "--"
            return f"{afr / self.wb_stoich:.3f}" if self.wb_show_lambda else f"{afr:.1f}"

        def tick():
            if not win.winfo_exists():
                return
            r = self.wb_relay
            now = time.monotonic()
            unit = "lambda" if self.wb_show_lambda else "AFR"
            for i in range(2):
                ch = None
                if r:
                    for c in r.chans:
                        if c.idx == i + 1:
                            ch = c
                if ch is None:
                    lbl_afr[i].config(text="--")
                    lbl_st[i].config(text="off" if not r else "no port set")
                else:
                    fresh = ch.afr is not None and now - ch.t < WB_STALE_S
                    lbl_afr[i].config(text=fmt(ch.afr) if fresh else "--")
                    lbl_st[i].config(text=(ch.state[:24] if not ch.state.startswith("CONNECTED") else ("OK " + unit if fresh else "connected, no data")))
            lbl_state.config(text=f"Relay: {self.wb_state}" + (f" | lines sent {r.sent}" if r else ""))
            cv.delete("all")
            w, h = max(cv.winfo_width(), 50), max(cv.winfo_height(), 50)
            lo, hi = (0.6, 1.4) if self.wb_show_lambda else (9.0, 20.0)
            for k, col in ((0, "#00e676"), (1, "#40c4ff")):
                if not r:
                    break
                pts = [(t, a) for (t, a) in r.history[k] if now - t <= 60.0]
                if len(pts) < 2:
                    continue
                xy = []
                for t, a in pts:
                    v = a / self.wb_stoich if self.wb_show_lambda else a
                    xy += [w - (now - t) / 60.0 * w, h - (min(max(v, lo), hi) - lo) / (hi - lo) * h]
                cv.create_line(*xy, fill=col, width=2)
            cv.create_text(6, 4, anchor="nw", fill="#888", text=f"{hi:g}")
            cv.create_text(6, h - 4, anchor="sw", fill="#888", text=f"{lo:g}   (last 60 s)")
            win.after(200, tick)

        tick()

    def show_rpm_dialog(self):
        """RPM calibration.  The firmware measures how often the ECU runs its crank interrupt routine; one known RPM turns
        that into a number K.  Hold a steady RPM (read it from the car's tach), type it here, press Calibrate (takes 10 s)."""
        win = tk.Toplevel(self)
        win.title("RPM calibration")
        win.geometry("520x250")
        f = ttk.Frame(win, padding=10)
        f.pack(fill=tk.BOTH, expand=True)
        ttk.Label(f, text="1. Connect port B.  2. Hold a steady engine speed (2000-3000 is good).\n"
                          "3. Type the real RPM and press Calibrate.  Wait 10 s.\n"
                          "4. Write the K number into compile.bat when it asks, so the tachometer works after a restart.",
                  justify=tk.LEFT).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=8)
        ttk.Label(row, text="Real RPM:").pack(side=tk.LEFT)
        ent = ttk.Entry(row, width=7)
        ent.insert(0, str(self.rpm_real))
        ent.pack(side=tk.LEFT, padx=4)

        def send(cmd):
            if not self.connected:
                messagebox.showinfo("RPM", "Press Connect (port B) first.")
                return
            try:
                self._ser_write(cmd.encode())
            except Exception as err:
                messagebox.showerror("RPM", str(err))

        def calibrate():
            try:
                v = int(ent.get())
            except ValueError:
                return
            if 300 <= v <= 9000:
                self.rpm_real = v
                self.save_settings()
                send(f"RC {v}\n")

        ttk.Button(row, text="Calibrate (10 s)", command=calibrate).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="Show RPM info", command=lambda: send("RPM\n")).pack(side=tk.LEFT, padx=4)
        lbl = ttk.Label(f, text="", font=("Consolas", 11), wraplength=480, justify=tk.LEFT)
        lbl.pack(anchor="w", pady=6)

        def tick():
            if not win.winfo_exists():
                return
            lbl.config(text=(self.rpm_text or "(no answer yet)") + f"\nSaved K: {self.rpm_k or 'none'}  (sent to the One ROM at every Connect)")
            win.after(500, tick)
        tick()

    def start_record(self):
        if not self.connected:
            messagebox.showinfo("Record", "Press Connect first.")
            return
        if self.recording:
            return
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(LOG_DIR, f"eprom_trace_{stamp}.csv")        # new file for every recording
            f = open(path, "w", encoding="utf-8", buffering=1 << 16, newline="")
            f.write(LOG_HEADER)
        except Exception as err:
            messagebox.showerror("Record", f"Cannot create the log file in\n{LOG_DIR}\n\n{err}")
            return
        with self.rec_lock:
            self.rec_path = path
            self.rec_file = f
            self.rec_seq = 0
            self.rec_stats = {}
            self.rec_lines = 0
            self.rec_t0 = time.monotonic()
            self.rec_flush_t = self.rec_t0
            self.last_rec_path = path
            self.recording = True
        self._blink_on = False
        self._blink_rec_now()
        self.status_bar.config(text=f" Recording to {path}")

    def _blink_rec_now(self):
        self.btn_record.config(style="RecOn.TButton", text="⏺ RECORDING")

    def _blink_rec(self):
        """Record button: red and blinking slowly while a recording is running, normal otherwise."""
        if self.recording:
            self._blink_on = not self._blink_on
            self.btn_record.config(style="RecOn.TButton" if self._blink_on else "RecDim.TButton", text="⏺ RECORDING")
        else:
            if self.btn_record.cget("text") != "⏺ Record":
                self.btn_record.config(style="TButton", text="⏺ Record")
        self.after(600, self._blink_rec)

    def stop_record(self, quiet=False):
        with self.rec_lock:
            was = self.recording
            self.recording = False
            f, self.rec_file = self.rec_file, None
            stats, self.rec_stats = self.rec_stats, {}
        self._wb_close_file()
        if f:
            try:
                f.close()
            except Exception:
                pass
        summary = None
        if was and stats:
            summary = self._write_summary(stats)
        if was and not quiet:
            self.status_bar.config(text=f" Recording stopped: {self.rec_lines} hits in {self.last_rec_path}"
                                        + (f" | summary: {summary}" if summary else ""))

    def _write_summary(self, stats):
        """One line per address that was read: hits, first/last time, the byte in the loaded BIN and the XDF map it belongs to.
        Addresses with mapped=NO are the ones the XDF does not describe yet."""
        path = self.last_rec_path[:-4] + "_summary.csv"
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["addr", "hits", "first_s", "last_s", "bin_byte", "mapped", "xdf_map"])
                for a in sorted(stats):
                    n, t1, t2 = stats[a]
                    tag = self._cell_xdf.get(a)
                    title = ""
                    if tag:
                        try:
                            title = self.xdf_maps[int(tag.split("_")[-1])]["title"]
                        except Exception:
                            title = ""
                    byte = f"{self.bin_data[a]:02X}" if a < self.EPROM_SIZE else ""
                    w.writerow([f"{a:04X}", n, f"{t1:.3f}", f"{t2:.3f}", byte, "YES" if tag else "NO", title])
            return path
        except Exception:
            return None

    def _write_frame(self, host_t, dev_ms, addrs):
        """Called from the reader thread: appends one frame to the log file (buffered; flushed once a second)."""
        with self.rec_lock:
            f = self.rec_file
            if not (self.recording and f):
                return
            rel = host_t - self.rec_t0
            wall = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            seq = self.rec_seq
            lines = []
            st = self.rec_stats
            for a in addrs:
                lines.append(f"{seq},{wall},{rel:.3f},{dev_ms},{a:04X}\n")
                seq += 1
                e = st.get(a)
                if e is None:
                    st[a] = [1, rel, rel]
                else:
                    e[0] += 1
                    e[2] = rel
            self.rec_seq = seq
            self.rec_lines += len(lines)
            try:
                f.writelines(lines)
                if host_t - self.rec_flush_t >= 1.0:
                    f.flush()
                    self.rec_flush_t = host_t
            except Exception as err:
                self.recording = False
                self.q.put(("err", f"log file write failed: {err}"))

    def toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.config(text="▶ Resume" if self.paused else "⏸ Pause")
        self.status_bar.config(text=" Paused: incoming addresses are ignored (not shown, not recorded)." if self.paused else " Resumed.")

    def _reader_thread(self, ser):
        buf = bytearray()
        last_hs = 0.0
        while not self.stop_evt.is_set():
            try:
                if not self.hello and time.monotonic() - last_hs > 1.0:        # repeat the handshake until the device answers
                    self._ser_write(b"TRACE\n")
                    last_hs = time.monotonic()
                data = ser.read(4096)
            except Exception as err:
                self.q.put(("err", str(err)))
                return
            if data:
                buf += data
            while buf:
                b0 = buf[0]
                if b0 == 0xA7:
                    if len(buf) < 6:
                        break
                    n = buf[1]
                    need = 6 + 2 * n
                    if len(buf) < need:
                        break
                    dev_ms = int.from_bytes(buf[2:6], "little")
                    self.rx_frames += 1
                    addrs = [buf[6 + 2 * i] | (buf[7 + 2 * i] << 8) for i in range(n)]
                    now_t = time.monotonic()
                    if not self.paused:
                        self._write_frame(now_t, dev_ms, addrs)             # recording never waits for the window
                    if self.q.qsize() < 300:                                # the live view may skip frames when the window is busy
                        self.q.put(("hits", now_t, dev_ms, addrs))
                    del buf[:need]
                elif b0 == 0xA8:
                    if len(buf) < 5:
                        break
                    self.q.put(("hb", buf[1] | (buf[2] << 8), buf[3] | (buf[4] << 8)))
                    del buf[:5]
                elif buf.startswith(b"SEL OK\n"):
                    self.q.put(("sel", True)); del buf[:7]
                elif buf.startswith(b"SEL NONE\n"):
                    self.q.put(("sel", False)); del buf[:9]
                elif buf.startswith(b"WB ON\n"):
                    self.q.put(("wb", "ON")); del buf[:6]
                elif buf.startswith(b"WB OFF\n"):
                    self.q.put(("wb", "OFF")); del buf[:7]
                elif buf.startswith(b"WB NONE\n"):
                    self.q.put(("wb", "NONE")); del buf[:8]
                elif buf.startswith((b"RK ", b"RPM ", b"RC WAIT")):
                    nl = buf.find(b"\n")
                    if nl < 0:
                        if len(buf) > 100:
                            del buf[0]              # no line end: not a text line, resynchronise
                            continue
                        break
                    self.q.put(("rpm", bytes(buf[:nl]).decode("ascii", "ignore").strip())); del buf[:nl + 1]
                elif buf.startswith(b"OR-TRACE2\n"):
                    self.q.put(("hello",))
                    del buf[:10]
                elif len(buf) < 10 and any(m.startswith(bytes(buf)) for m in (b"OR-TRACE2\n", b"SEL OK\n", b"SEL NONE\n", b"WB ON\n", b"WB OFF\n", b"WB NONE\n", b"RK ", b"RPM ", b"RC WAIT")):
                    break
                else:
                    del buf[0]                      # not a frame: resynchronise

    def _poll(self):
        changed = False
        t_poll = time.monotonic()
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "hits":
                    _, host_t, dev_ms, addrs = item
                    if self.paused:
                        continue
                    self.frames += 1
                    for a in addrs:
                        self.last_seen[a] = host_t
                        self.hit_count[a] = self.hit_count.get(a, 0) + 1
                    changed = True
                    if time.monotonic() - t_poll > 0.12:                   # keep the window responsive: rest next time
                        break
                elif kind == "hb":
                    self.dev_drop, self.dev_over = item[1], item[2]
                    self.last_hb_t = time.monotonic()
                elif kind == "sel":
                    self.status_bar.config(text=" Data trace now follows this table in TunerPro." if item[1]
                                           else " The firmware does not trace this table (it traces the VE table).")
                elif kind == "rpm":
                    self.rpm_text = item[1]
                    mk = re.match(r"RK (\d+)$", item[1])
                    if mk and int(mk.group(1)) > 0:             # calibration finished: remember it, it is sent again on every Connect
                        self.rpm_k = int(mk.group(1))
                        self.save_settings()
                    self.status_bar.config(text=" One ROM: " + item[1])
                elif kind == "wb":
                    self._wb_reply(item[1])
                elif kind == "hello":
                    self.hello = True
                    self.status_bar.config(text=" Device answered: port B is streaming addresses.")
                elif kind == "err":
                    self.status_bar.config(text=f" Port error: {item[1]}")
                    self.disconnect()
                    messagebox.showerror("Port B", f"Connection lost:\n{item[1]}")
        except queue.Empty:
            pass
        self._monitor_link()
        if not self.paused:
            self._refresh_live(force=False)
        if self.connected:
            state = "PAUSED" if self.paused else ("streaming" if self.hello else "waiting for handshake answer...")
            rec = f"REC {self.rec_lines} lines" if self.recording else "not recording"
            self.lbl_live.config(text=f"{state} | {rec} | unique {len(self.hit_count)} | device dropped {self.dev_drop}, overruns {self.dev_over}")
        else:
            self.lbl_live.config(text="not connected")
        self.after(250, self._poll)

    def _log_error(self, kind, msg):
        """Append one line to eprom_viewer_error.log (same file as program errors) and count it."""
        self.err_count += 1
        try:
            with open(ERR_LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  PORTB {kind}: {msg}\n")
        except Exception:
            pass

    def _monitor_link(self):
        """Called from _poll.  Link alive = the device's once-a-second health frame.  Packet rate = address frames per second
        compared with the recent typical rate; a clear drop (or silence) is written to the error log."""
        now = time.monotonic()
        if not self.connected:
            self.lbl_link.config(text="  \u25cf port B: not connected  ", bg="#707070")
            return
        if now - self.last_hb_t > 3.0 and self.hello:
            if not self.link_alarm:
                self.link_alarm = True
                self._log_error("LINK SILENT", f"no health frame from the device for {now - self.last_hb_t:.1f} s (USB reset or cable?)")
        elif self.link_alarm and now - self.last_hb_t < 2.0:
            self.link_alarm = False
            self._log_error("LINK BACK", "health frames are arriving again")
        if now - self.rate_t >= 1.0:
            n = self.rx_frames - self.rate_n
            rate = n / (now - self.rate_t)
            self.rate_t, self.rate_n = now, self.rx_frames
            self.cur_rate = rate
            if rate > 20.0 and not self.rate_alarm and (self.rate_base == 0.0 or rate >= 0.5 * self.rate_base):
                self.rate_base = rate if self.rate_base == 0.0 else self.rate_base * 0.9 + rate * 0.1
            if self.rate_base > 20.0 and rate < 0.5 * self.rate_base:
                self.rate_low += 1
                if self.rate_low >= 2 and not self.rate_alarm:
                    self.rate_alarm = True
                    self._log_error("RATE DROP", f"{rate:.0f} frames/s, typical {self.rate_base:.0f}/s (engine stopped also does this)")
            else:
                self.rate_low = 0
                if self.rate_alarm and rate > 0.8 * self.rate_base:
                    self.rate_alarm = False
                    self._log_error("RATE BACK", f"{rate:.0f} frames/s")
            if self.dev_drop != self.last_drop:
                if (self.dev_drop - self.last_drop) & 0xFFFF > 200:
                    self._log_error("DEVICE DROP", f"device dropped {(self.dev_drop - self.last_drop) & 0xFFFF} addresses in the last second (total {self.dev_drop})")
                self.last_drop = self.dev_drop
        if self.link_alarm:
            txt, col = f"  \u25cf port B: LINK LOST ({self.err_count} logged)  ", "#c62828"
        elif not self.hello:
            txt, col = "  \u25cf port B: waiting for device  ", "#f9a825"
        elif self.rate_alarm:
            txt, col = f"  \u25cf port B: rate low {getattr(self, 'cur_rate', 0):.0f}/s ({self.err_count} logged)  ", "#ef6c00"
        else:
            txt, col = f"  \u25cf port B: OK {getattr(self, 'cur_rate', 0):.0f} frames/s" + (f" ({self.err_count} logged)" if self.err_count else "") + "  ", "#2e7d32"
        if self.wb_relay:
            txt += "AEM on  "
        self.lbl_link.config(text=txt, bg=col)

    def _refresh_live(self, force):
        if not self.live_enabled:
            return
        if not self.connected and not self.last_seen:      # nothing live: leave a loaded log overlay alone
            return
        now = time.monotonic()
        if self.live_window > 0:
            want = {a for a, t in self.last_seen.items() if now - t <= self.live_window and a < self.EPROM_SIZE}
        else:
            want = {a for a in self.last_seen if a < self.EPROM_SIZE}
        if not force and want == self.shown:
            return
        add, drop = want - self.shown, self.shown - want
        # at most this many cell edits per call: a busy ECU hits thousands of addresses, the rest follows on the next timer tick
        todo = [(a, False) for a in drop] + [(a, True) for a in add]
        batch = todo[:300]
        self.hex_text.config(state=tk.NORMAL)
        try:
            for a, hit in batch:
                self._draw_cell(a, hit)
                if hit:
                    self.shown.add(a)
                else:
                    self.shown.discard(a)
        finally:
            self.hex_text.config(state=tk.DISABLED)
        self.accessed_addresses = set(self.shown)

    def _draw_cell(self, addr, hit):
        """Redraws ONE byte (hex part and ascii part) in place; the text widget must be in NORMAL state."""
        t = self.hex_text
        line = (addr // 16) + 1
        hs, he, as_, ae = self._get_char_indices_for_byte(addr % 16)
        xt = self._cell_xdf.get(addr)
        sel = self.selected_address == addr
        if self.log_cover_text:
            val = self.bin_data[addr]
            hex_txt = f"{self.log_dot_symbol} " if hit else f"{val:02X}"
            asc_txt = self.log_dot_symbol if hit else (chr(val) if 32 <= val <= 126 else ".")
            htags = tuple(x for x in (xt, "log_hit" if hit else None, "selected_cell" if sel else None) if x)
            atags = tuple(x for x in ("ascii_col", xt, "log_dot" if hit else None, "selected_cell" if sel else None) if x)
            # ascii first (it is to the right), so the left indices stay valid
            t.delete(f"{line}.{as_}", f"{line}.{ae}")
            t.insert(f"{line}.{as_}", asc_txt, atags)
            t.delete(f"{line}.{hs}", f"{line}.{he}")
            t.insert(f"{line}.{hs}", hex_txt, htags)
        else:
            for tag, a, b in (("log_hit", hs, he), ("log_dot", as_, ae)):
                (t.tag_add if hit else t.tag_remove)(tag, f"{line}.{a}", f"{line}.{b}")
        if hit:
            t.tag_configure("log_hit", foreground=self.log_highlight_color, font=("Consolas", 11, "bold"))
            t.tag_configure("log_dot", foreground=self.log_dot_color, font=("Segoe UI", self.log_dot_size, "bold"))
            t.tag_raise("log_hit")
            t.tag_raise("log_dot")

    def save_log_as(self):
        src = self.last_rec_path
        if not src or not os.path.exists(src):
            messagebox.showinfo("Save log", "Nothing recorded yet. Press Connect, then Record.")
            return
        if self.rec_file:
            self.rec_file.flush()
        dst = filedialog.asksaveasfilename(
            title="Save recorded log as", defaultextension=".csv", initialfile=os.path.basename(src),
            filetypes=[("CSV log", "*.csv"), ("All Files", "*.*")])
        if not dst:
            return
        try:
            shutil.copyfile(src, dst)
            self.status_bar.config(text=f" Log saved to {dst} (the running recording is also kept in {src})")
        except Exception as err:
            messagebox.showerror("Save log", str(err))

    def _autosave_tick(self):
        """Every 30 s: remember the window size and write all settings (ports, calibration, files)."""
        try:
            self.win_geometry = self.geometry()
            self.save_settings()
        except Exception:
            pass
        self.after(30000, self._autosave_tick)

    def on_close(self):
        try:
            self.win_geometry = self.geometry()
            self.save_settings()
        except Exception:
            pass
        self._wb_stop(send_off=True)
        if self.connected:
            self.disconnect()
        self.destroy()

    def goto_address(self):
        raw = self.entry_goto.get().strip()
        if not raw:
            return
        try:
            addr = int(raw, 16) if not raw.startswith("0x") else int(raw, 16)
            if 0 <= addr < self.EPROM_SIZE:
                line_num = (addr // 16) + 1
                self.hex_text.see(f"{line_num}.0")
                self.selected_address = addr
                self._highlight_selected_cell(addr)
                self._update_same_line_map_info(addr)
                self.status_bar.config(text=f" Jumped to address: 0x{addr:04X}")
            else:
                messagebox.showwarning("Out of Range", f"Address 0x{addr:X} exceeds 64KB EPROM range.")
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter a valid Hexadecimal address (e.g. E26C).")


if __name__ == "__main__":
    app = EPROMHexViewer()
    app.mainloop()
