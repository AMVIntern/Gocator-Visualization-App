import os
import re
import csv
import queue
import logging
from logging.handlers import RotatingFileHandler
import tkinter as tk
from datetime import datetime
from PIL import Image, ImageTk
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- CONFIGURATION ---
FOLDER_IMG_L = r"D:\Gocator_viz\Gocator_Top_Image"
FOLDER_IMG_R = r"D:\Gocator_viz\Gocator_Bottom_Image"
FOLDER_CSV_L = r"D:\Gocator_viz\Gocator_Top_Result"
FOLDER_CSV_R = r"D:\Gocator_viz\Gocator_Bottom_Result"
SUPPORTED_IMGS = (".png", ".jpg", ".jpeg", ".bmp")

APP_TITLE = "CHEP PQAS"

LOGO_LEFT_PATH = r"D:\Gocator_viz\LOGOS\Splash.png"
LOGO_RIGHT_PATH = r"D:\Gocator_viz\LOGOS\ChepLogo.png"

LOG_PATH = r"D:\Gocator_viz\monitor.log"

# Retry settings for files still being written
READ_RETRIES = 10
READ_RETRY_DELAY_MS = 150

# Top & bottom files for the SAME pallet are written within ~40 ms of each
# other (measured), while consecutive pallets are >= 2.8 s apart. So two
# results whose filename timestamps differ by <= PAIR_TOLERANCE_S belong to
# the same pallet; anything further apart is a different pallet.
PAIR_TOLERANCE_S = 1.5

# Filename pattern: result_YYYYMMDD_HHMMSS_microseconds.csv
FNAME_TS_RE = re.compile(r"result_(\d{8}_\d{6}_\d+)\.csv$", re.IGNORECASE)

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
        self._border_color = C_STROKE
        self._status_text = "OK"
        self._status_color = C_OK
        self._img_pil = None

    def set_status(self, ok=True):
        if ok:
            self._status_text = "OK"
            self._status_color = C_OK
            self._border_color = C_STROKE
        else:
            self._status_text = "AOI"
            self._status_color = C_AO
            self._border_color = C_AO
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
        round_rect(self.cv, pad, pad, W - pad, H - pad, r=r,
                   fill=C_PANEL, outline=self._border_color, width=2)

        header_h = 34
        round_rect(self.cv, pad+2, pad+2, W-pad-2, pad+header_h, r=r-6,
                   fill=C_PANEL_2, outline="", width=0)

        self.cv.create_text(pad + 16, pad + 18, text=self.title, fill=C_TEXT,
                            font=("Segoe UI", 11, "bold"), anchor="w")

        self.cv.create_rectangle(W - pad - 56, pad + 10, W - pad - 14, pad + 26,
                                 outline="", fill=self._status_color)
        self.cv.create_text(W - pad - 35, pad + 18, text=self._status_text,
                            fill=C_BLACK, font=("Segoe UI", 10, "bold"), anchor="c")

        img_x1, img_y1 = pad + 14, pad + header_h + 12
        img_x2, img_y2 = W - pad - 14, H - pad - 14

        self.cv.create_rectangle(img_x1, img_y1, img_x2, img_y2,
                                 outline=C_STROKE_D, width=2, fill="#050a12")

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

        self.event_queue = queue.Queue()

        self._build_layout()
        self.start_monitoring()
        self._poll_queue()

    def _build_layout(self):
        PAD = 12
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        main = tk.Frame(self.root, bg=C_BG)
        main.grid(row=0, column=0, sticky="nsew")
        main.grid_rowconfigure(0, weight=0)
        main.grid_rowconfigure(1, weight=1)
        main.grid_rowconfigure(2, weight=0)
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

        self.tile_left = ImageTile(main, title="Gocator_Top_Image")
        self.tile_left.grid(row=1, column=0, sticky="nsew", padx=(PAD, PAD//2), pady=(6, 6))

        self.tile_right = ImageTile(main, title="Gocator_Bottom_Image")
        self.tile_right.grid(row=1, column=1, sticky="nsew", padx=(PAD//2, PAD), pady=(6, 6))

        self.status_lbl = tk.Label(main, text="", font=("Segoe UI", 54, "bold"),
                                   bg="#0b1220", fg="#0b1220")
        self.status_lbl.grid(row=2, column=0, columnspan=2, sticky="nsew",
                             padx=PAD, pady=(6, PAD))

    def _draw_title_box(self, canvas: tk.Canvas):
        canvas.delete("all")
        W, H = canvas.winfo_width(), canvas.winfo_height()
        if W <= 10 or H <= 10:
            return
        round_rect(canvas, 4, 4, W-4, H-4, r=18, fill=C_PANEL, outline=C_STROKE, width=2)
        canvas.create_text(W/2, H/2, text=APP_TITLE, fill=C_TEXT,
                           font=("Segoe UI", 30, "bold"), anchor="c")

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

    def render_combined_status(self, top, bottom):
        assured = (top == 1 and bottom == 1)
        log.info("Combined verdict: top=%s bottom=%s -> %s",
                 top, bottom, "Assured" if assured else "Standard")
        if assured:
            self.status_lbl.config(bg=C_ASSURED, fg="white", text="Assured")
        else:
            self.status_lbl.config(bg=C_STANDARD, fg="white", text="Standard")

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