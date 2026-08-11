import os
import re
import csv
import json
import queue
import logging
from logging.handlers import RotatingFileHandler
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from PIL import Image, ImageTk
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dotenv import load_dotenv

# --- CONFIGURATION ---
# All settings come from config.env (tracked in git). Every key must be
# present — there are no in-code fallbacks; a missing key fails on startup.
load_dotenv(Path(__file__).parent / "config.env")

FOLDER_IMG_L = os.environ["FOLDER_IMG_TOP"]
FOLDER_IMG_R = os.environ["FOLDER_IMG_BOTTOM"]
FOLDER_CSV_L = os.environ["FOLDER_CSV_TOP"]
FOLDER_CSV_R = os.environ["FOLDER_CSV_BOTTOM"]
SUPPORTED_IMGS = (".png", ".jpg", ".jpeg", ".bmp")

APP_TITLE = "CHEP PQAS"

LOGO_LEFT_PATH = os.environ["LOGO_LEFT_PATH"]
LOGO_RIGHT_PATH = os.environ["LOGO_RIGHT_PATH"]

LOG_PATH = os.environ["LOG_PATH"]
STATE_PATH = os.environ["STATE_PATH"]

# Shift start times (HH:MM, 24-hour) — used by the per-shift counter (Phase 3).
SHIFT1_START = os.environ["SHIFT1_START"]
SHIFT2_START = os.environ["SHIFT2_START"]
SHIFT3_START = os.environ["SHIFT3_START"]

# Retry settings for files still being written
READ_RETRIES = int(os.environ["READ_RETRIES"])
READ_RETRY_DELAY_MS = int(os.environ["READ_RETRY_DELAY_MS"])

# Top & bottom files for the SAME pallet are written within ~40 ms of each
# other (measured), while consecutive pallets are >= 2.8 s apart. So two
# results whose filename timestamps differ by <= PAIR_TOLERANCE_S belong to
# the same pallet; anything further apart is a different pallet.
PAIR_TOLERANCE_S = float(os.environ["PAIR_TOLERANCE_S"])

# Filename pattern: result_YYYYMMDD_HHMMSS_microseconds.csv
FNAME_TS_RE = re.compile(r"result_(\d{8}_\d{6}_\d+)\.csv$", re.IGNORECASE)

# ----------------------------
# SHIFTS
# ----------------------------
# How often (ms) to re-check whether the shift has rolled over.
SHIFT_CHECK_MS = 30 * 1000


def _parse_hhmm(s):
    """'HH:MM' -> minutes since midnight."""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# Shift start times from config, sorted so the last one is the overnight shift
# that wraps past midnight (e.g. 22:00 -> 06:00).
SHIFT_STARTS = sorted(
    [("Shift 1", _parse_hhmm(SHIFT1_START)),
     ("Shift 2", _parse_hhmm(SHIFT2_START)),
     ("Shift 3", _parse_hhmm(SHIFT3_START))],
    key=lambda x: x[1],
)


def current_shift(now=None):
    """Name of the shift active at ``now`` (defaults to the current time)."""
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    name = SHIFT_STARTS[-1][0]  # overnight shift, unless a later start matches
    for cand, start in SHIFT_STARTS:
        if minutes >= start:
            name = cand
        else:
            break
    return name


def shift_start_dt(now=None):
    """``datetime`` at which the currently-active shift began. For the
    overnight shift between midnight and its start, this is the previous day."""
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    start_min = SHIFT_STARTS[-1][1]
    started_today = False
    for _cand, start in SHIFT_STARTS:
        if minutes >= start:
            start_min = start
            started_today = True
        else:
            break
    h, m = divmod(start_min, 60)
    start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if not started_today:
        start_dt -= timedelta(days=1)
    return start_dt

# ----------------------------
# LOGGING (rotating: 10 MB per file, 5 backups kept -> ~60 MB max on disk)
# ----------------------------
log = logging.getLogger("gocator")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(
    LOG_PATH,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)

# ----------------------------
# THEME
# ----------------------------
C_BG        = "#0b1220"
C_PANEL     = "#0f1a2b"
C_PANEL_2   = "#0c1525"
C_STROKE    = "#00c2c7"
C_STROKE_D  = "#0a6f73"
C_TEXT      = "#e6f1ff"
C_MUTED     = "#17e6a1"
C_OK        = "#17e6a1"
C_AO        = "#ff4d6d"
C_BLACK     = "#000000"

C_ASSURED   = "#0A3DFF"
C_STANDARD  = "#7a7a7a"


def round_rect(canvas, x1, y1, x2, y2, r=16, **kwargs):
    r = max(0, min(r, int((x2 - x1) / 2), int((y2 - y1) / 2)))
    points = [
        x1+r, y1, x2-r, y1, x2, y1, x2, y1+r,
        x2, y2-r, x2, y2, x2-r, y2, x1+r, y2,
        x1, y2, x1, y2-r, x1, y1+r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def file_timestamp(path):
    """Timestamp identifying which pallet/cycle a result file belongs to.
    Prefer the timestamp embedded in the filename; fall back to mtime."""
    m = FNAME_TS_RE.search(os.path.basename(path))
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S_%f")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return datetime.now()


class UniversalHandler(FileSystemEventHandler):
    """Pushes events into a thread-safe queue. Never touches Tk from here —
    watchdog runs on its own thread and Tkinter is not thread-safe."""
    def __init__(self, event_queue, side, file_type):
        self.event_queue = event_queue
        self.side = side
        self.file_type = file_type

    def on_created(self, event):
        if not event.is_directory:
            self.event_queue.put((event.src_path, self.side, self.file_type))


class ImageTile(tk.Frame):
    def __init__(self, parent, title="Tile", **kwargs):
        super().__init__(parent, bg=C_BG, **kwargs)
        self.title = title
        self.photo = None
        self.cv = tk.Canvas(self, bg=C_BG, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Configure>", self._redraw)
        self._ok = True
        self._status_text = "OK"
        self._status_color = C_OK
        self._img_pil = None

    def set_status(self, ok=True):
        self._ok = ok
        self._status_text = "OK" if ok else "AOI"
        self._status_color = C_OK if ok else C_AO
        self._redraw()

    def set_image(self, pil_img: Image.Image):
        self._img_pil = pil_img
        self._redraw()

    def _redraw(self, event=None):
        self.cv.delete("all")
        W = self.cv.winfo_width()
        H = self.cv.winfo_height()
        if W <= 10 or H <= 10:
            return

        pad, r = 10, 18
        # Outer panel border: teal always (status is shown on the image frame).
        round_rect(self.cv, pad, pad, W - pad, H - pad, r=r,
                   fill=C_PANEL, outline=C_STROKE, width=2)

        header_h = 34
        round_rect(self.cv, pad+2, pad+2, W-pad-2, pad+header_h, r=r-6,
                   fill=C_PANEL_2, outline="", width=0)

        # Green "active" dot + title (top-left)
        dot = 9
        dy = pad + 17
        self.cv.create_rectangle(pad + 14, dy - dot//2, pad + 14 + dot, dy + dot//2,
                                 outline="", fill=C_OK)
        self.cv.create_text(pad + 14 + dot + 8, pad + 18, text=self.title,
                            fill=C_TEXT, font=("Segoe UI", 11, "bold"), anchor="w")

        # OK / AOI status as coloured text (top-right)
        self.cv.create_text(W - pad - 16, pad + 18, text=self._status_text,
                            fill=self._status_color, anchor="e",
                            font=("Segoe UI", 11, "bold"))

        img_x1, img_y1 = pad + 14, pad + header_h + 12
        img_x2, img_y2 = W - pad - 14, H - pad - 14

        # Image frame: green when OK, red when AOI.
        frame_col = C_OK if self._ok else C_AO
        self.cv.create_rectangle(img_x1, img_y1, img_x2, img_y2,
                                 outline=frame_col, width=3, fill="#050a12")

        if self._img_pil is None:
            self.cv.create_text((img_x1 + img_x2) / 2, (img_y1 + img_y2) / 2,
                                text="Waiting for Image...", fill=C_MUTED,
                                font=("Segoe UI", 14, "bold"))
            return

        area_w = int(img_x2 - img_x1 - 8)
        area_h = int(img_y2 - img_y1 - 8)
        if area_w <= 10 or area_h <= 10:
            return

        img = self._img_pil.copy()
        img.thumbnail((area_w, area_h))
        self.photo = ImageTk.PhotoImage(img)
        self.cv.create_image((img_x1 + img_x2) / 2, (img_y1 + img_y2) / 2,
                             image=self.photo)


class MonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Gocator Live Inspector")
        self.root.configure(bg=C_BG)

        # Pending result per side for the current pallet: (timestamp, value)
        # Timestamp comes from the FILENAME, so pairing is immune to
        # arrival-order races and to one camera skipping a pallet.
        self.pending = {"left": None, "right": None}

        # Per-shift pallet counters. Incremented once per completed (paired)
        # pallet; reset to zero when the shift rolls over.
        self.total_pallets = 0
        self.assured_pallets = 0
        self.active_shift = current_shift()
        self._load_state()  # restore counts if a saved state matches this shift

        self.event_queue = queue.Queue()

        self._build_layout()
        self._update_metrics_display()  # show restored/zero counts at startup
        self.start_monitoring()
        self._poll_queue()
        self._poll_shift()

    def _build_layout(self):
        PAD = 12
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        main = tk.Frame(self.root, bg=C_BG)
        main.grid(row=0, column=0, sticky="nsew")
        main.grid_rowconfigure(0, weight=0)   # header
        main.grid_rowconfigure(1, weight=0)   # station sub-header bar
        main.grid_rowconfigure(2, weight=1)   # image tiles
        main.grid_rowconfigure(3, weight=0)   # verdict banner
        main.grid_columnconfigure(0, weight=1, uniform="cols")
        main.grid_columnconfigure(1, weight=1, uniform="cols")

        header = tk.Frame(main, bg=C_BG)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=PAD, pady=(PAD, 6))
        header.grid_columnconfigure(0, weight=0)
        header.grid_columnconfigure(1, weight=1)
        header.grid_columnconfigure(2, weight=0)

        self.left_logo = self._logo_box(header, LOGO_LEFT_PATH, "AUSTRALIAN\nMACHINE\nVISION")
        self.left_logo.grid(row=0, column=0, sticky="w", padx=(0, 10))

        title_box = tk.Canvas(header, height=70, bg=C_BG, highlightthickness=0)
        title_box.grid(row=0, column=1, sticky="ew")
        title_box.bind("<Configure>", lambda e: self._draw_title_box(title_box))

        self.right_logo = self._logo_box(header, LOGO_RIGHT_PATH, "CHEP")
        self.right_logo.grid(row=0, column=2, sticky="e", padx=(10, 0))

        # Full-width station sub-header bar
        self.station_bar = tk.Canvas(main, height=32, bg=C_BG, highlightthickness=0)
        self.station_bar.grid(row=1, column=0, columnspan=2, sticky="ew",
                              padx=PAD, pady=(0, 6))
        self.station_bar.bind("<Configure>", lambda e: self._draw_station_bar())

        self.tile_left = ImageTile(main, title="Gocator_Top_Image")
        self.tile_left.grid(row=2, column=0, sticky="nsew", padx=(PAD, PAD//2), pady=(6, 6))

        self.tile_right = ImageTile(main, title="Gocator_Bottom_Image")
        self.tile_right.grid(row=2, column=1, sticky="nsew", padx=(PAD//2, PAD), pady=(6, 6))

        self.status_lbl = tk.Label(main, text="", font=("Segoe UI", 54, "bold"),
                                   bg="#0b1220", fg="#0b1220")
        self.status_lbl.grid(row=3, column=0, columnspan=2, sticky="nsew",
                             padx=PAD, pady=(6, PAD))

        # Per-shift counter, shown bottom-right inside the verdict banner.
        self.metrics_lbl = tk.Label(self.status_lbl, text="", justify="right",
                                    font=("Segoe UI", 14, "bold"),
                                    bg="#0b1220", fg="white")
        self.metrics_lbl.place(relx=1.0, rely=1.0, x=-26, y=-14, anchor="se")

    def _draw_title_box(self, canvas: tk.Canvas):
        canvas.delete("all")
        W, H = canvas.winfo_width(), canvas.winfo_height()
        if W <= 10 or H <= 10:
            return
        round_rect(canvas, 4, 4, W-4, H-4, r=18, fill=C_PANEL, outline=C_STROKE, width=2)
        canvas.create_text(W/2, H/2, text=APP_TITLE, fill=C_TEXT,
                           font=("Segoe UI", 30, "bold"), anchor="c")

    def _draw_station_bar(self):
        cv = self.station_bar
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        if W <= 10 or H <= 10:
            return
        round_rect(cv, 2, 2, W-2, H-2, r=12, fill=C_PANEL, outline=C_STROKE, width=1)
        cv.create_text(W/2, H/2, text="Gocator Station", fill=C_TEXT,
                       font=("Segoe UI", 13, "bold"), anchor="c")

    def _logo_box(self, parent, path, fallback_text):
        cv = tk.Canvas(parent, width=180, height=70, bg=C_BG, highlightthickness=0)
        def draw():
            cv.delete("all")
            W, H = cv.winfo_width(), cv.winfo_height()
            round_rect(cv, 4, 4, W-4, H-4, r=18, fill=C_PANEL, outline=C_STROKE, width=2)
            if path and os.path.exists(path):
                try:
                    img = Image.open(path)
                    img.thumbnail((W-16, H-16))
                    cv._photo = ImageTk.PhotoImage(img)
                    cv.create_image(W/2, H/2, image=cv._photo)
                    return
                except Exception:
                    pass
            cv.create_text(W/2, H/2, text=fallback_text, fill=C_MUTED,
                           font=("Segoe UI", 10, "bold"))
        cv.bind("<Configure>", lambda e: draw())
        draw()
        return cv

    # ----------------------------
    # Monitoring
    # ----------------------------
    def start_monitoring(self):
        self.observer = Observer()
        configs = [
            (FOLDER_IMG_L, "left", "img"),
            (FOLDER_IMG_R, "right", "img"),
            (FOLDER_CSV_L, "left", "csv"),
            (FOLDER_CSV_R, "right", "csv"),
        ]
        for path, side, ftype in configs:
            os.makedirs(path, exist_ok=True)
            handler = UniversalHandler(self.event_queue, side, ftype)
            self.observer.schedule(handler, path, recursive=False)
        self.observer.start()
        log.info("Monitoring started")

    def _poll_queue(self):
        """Runs on the Tk main thread; drains watchdog events safely."""
        try:
            while True:
                path, side, ftype = self.event_queue.get_nowait()
                if ftype == "img" and path.lower().endswith(SUPPORTED_IMGS):
                    self.update_image(path, side, attempt=0)
                elif ftype == "csv" and path.lower().endswith(".csv"):
                    self.update_result_from_csv(path, side, attempt=0)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_queue)

    # ----------------------------
    # Image handling (with retry for partially written files)
    # ----------------------------
    def update_image(self, path, side, attempt=0):
        try:
            img = Image.open(path)
            img.load()  # force full read NOW so truncated files raise here
        except Exception as e:
            if attempt < READ_RETRIES:
                self.root.after(READ_RETRY_DELAY_MS,
                                lambda: self.update_image(path, side, attempt + 1))
            else:
                log.error("Image read failed (%s) %s: %s", side, path, e)
            return

        if side == "left":
            self.tile_left.set_image(img)
        else:
            self.tile_right.set_image(img)

    # ----------------------------
    # CSV handling
    # ----------------------------
    def update_result_from_csv(self, path, side, attempt=0):
        try:
            with open(path, mode="r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                row = next(reader)
                row = {(k or "").strip().lower(): (v or "").strip()
                       for k, v in row.items()}
                result = int(row["results"])
        except (StopIteration, KeyError, ValueError, OSError) as e:
            if attempt < READ_RETRIES:
                self.root.after(READ_RETRY_DELAY_MS,
                                lambda: self.update_result_from_csv(path, side, attempt + 1))
            else:
                log.error("CSV read failed (%s) %s: %s", side, path, e)
            return

        ts = file_timestamp(path)
        log.info("Result %s %s -> %s", side, os.path.basename(path), result)

        # Update the tile immediately
        if side == "left":
            self.tile_left.set_status(ok=(result == 1))
        else:
            self.tile_right.set_status(ok=(result == 1))

        # --- Pairing by timestamp ---
        # If something older is already pending on OUR side, its partner
        # never arrived (camera skipped a pallet). Discard it.
        if self.pending[side] is not None:
            log.warning("Orphaned %s result discarded (partner never arrived): %s",
                        side, self.pending[side][0])
        self.pending[side] = (ts, result)

        other = "right" if side == "left" else "left"
        if self.pending[other] is not None:
            dt = abs((self.pending[other][0] - ts).total_seconds())
            if dt <= PAIR_TOLERANCE_S:
                # Same pallet -> render combined verdict
                top = self.pending["left"][1]
                bottom = self.pending["right"][1]
                self.render_combined_status(top, bottom)
                self.pending = {"left": None, "right": None}
            else:
                # The other side's result is from an OLDER pallet whose
                # partner never came. Discard it; keep waiting for ours.
                log.warning("Orphaned %s result discarded (timestamp gap %.1fs): %s",
                            other, dt, self.pending[other][0])
                self.pending[other] = None

    # ----------------------------
    # Shift roll-over
    # ----------------------------
    def _poll_shift(self):
        """Periodic check so the counters still reset on an idle line (no
        pallets arriving across the boundary). Runs on the Tk main thread."""
        self._check_shift()
        self.root.after(SHIFT_CHECK_MS, self._poll_shift)

    def _check_shift(self):
        """Reset the per-shift counters if the shift has rolled over since the
        last check. Idempotent — only acts on an actual change."""
        cur = current_shift()
        if cur != self.active_shift:
            log.info("Shift changed %s -> %s; counters reset",
                     self.active_shift, cur)
            self.active_shift = cur
            self.total_pallets = 0
            self.assured_pallets = 0
            self._save_state()
            self._update_metrics_display()

    # ----------------------------
    # Restart-safe persistence
    # ----------------------------
    def _save_state(self):
        """Persist the current shift's counts so they survive a restart.
        Atomic write (temp file + os.replace)."""
        data = {
            "shift": self.active_shift,
            "shift_start": shift_start_dt().isoformat(),
            "total": self.total_pallets,
            "assured": self.assured_pallets,
        }
        try:
            folder = os.path.dirname(STATE_PATH)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            log.error("Could not save shift state to %s: %s", STATE_PATH, e)

    def _load_state(self):
        """Restore counts from STATE_PATH, but only if the saved state belongs
        to the SAME shift occurrence (same name and start). Otherwise the
        counters stay at zero (fresh shift)."""
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return  # no/invalid state -> start fresh
        if (data.get("shift") == self.active_shift and
                data.get("shift_start") == shift_start_dt().isoformat()):
            self.total_pallets = int(data.get("total", 0))
            self.assured_pallets = int(data.get("assured", 0))
            log.info("Restored counts for %s: Total: %d  Assured: %d (%d%%)",
                     self.active_shift, self.total_pallets,
                     self.assured_pallets, self.assured_percent())
        else:
            log.info("Discarded stale shift state (was %s @ %s)",
                     data.get("shift"), data.get("shift_start"))

    def render_combined_status(self, top, bottom):
        # Reset first if a shift boundary was crossed, so a pallet completing
        # just after the boundary is counted in the NEW shift.
        self._check_shift()

        assured = (top == 1 and bottom == 1)

        # Count this completed pallet (every paired verdict counts once).
        self.total_pallets += 1
        if assured:
            self.assured_pallets += 1

        log.info("Combined verdict: top=%s bottom=%s -> %s | %s | "
                 "Total: %d  Assured: %d (%d%%)",
                 top, bottom, "Assured" if assured else "Standard",
                 self.active_shift,
                 self.total_pallets, self.assured_pallets, self.assured_percent())

        color = C_ASSURED if assured else C_STANDARD
        self.status_lbl.config(bg=color, fg="white",
                               text="Assured" if assured else "Standard")
        self.metrics_lbl.config(bg=color)   # keep overlay bg matching the banner
        self._update_metrics_display()

        self._save_state()  # persist so the count survives a restart

    def assured_percent(self):
        """Percentage of this shift's pallets deemed Assured (0 when none)."""
        if self.total_pallets == 0:
            return 0
        return round(100 * self.assured_pallets / self.total_pallets)

    def _update_metrics_display(self):
        """Refresh the pallet counters shown in the verdict banner."""
        self.metrics_lbl.config(
            text=f"Total Pallets Count: {self.total_pallets}\n"
                 f"Potential Assured Count: {self.assured_pallets} ({self.assured_percent()}%)")

    def on_close(self):
        try:
            self.observer.stop()
            self.observer.join()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = MonitorApp(root)
    root.update()
    root.state('zoomed')
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()