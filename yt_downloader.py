"""Media Downloader: видео/аудио с YouTube, TikTok, VK и др. сайтов (yt-dlp)
+ скачивание торрентов (magnet-ссылки и .torrent-файлы, libtorrent).

Запуск:  python yt_downloader.py  (или двойной клик по «Скачать (GUI).bat»)
Зависимости:
    pip install yt-dlp libtorrent
    ffmpeg в PATH (нужен для конвертации аудио и склейки видео)
    Node.js или Deno в PATH (JS-рантайм для YouTube)
"""

import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import yt_dlp

# в portable-exe настройки и журнал храним рядом с самим exe, а не во временной папке
APP_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))

# ffmpeg/ffprobe, встроенные в exe, лежат в корне временной папки распаковки
if getattr(sys, "frozen", False):
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
WINDOW_TITLE = "Media Downloader"

# высота -> ограничение в format-строке yt-dlp (None = без ограничения)
VIDEO_QUALITIES = {"Лучшее": None, "2160p (4K)": 2160, "1440p": 1440,
                   "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
AUDIO_FORMATS = ("mp3", "m4a", "opus", "wav", "flac")
AUDIO_QUALITY = {"mp3": "192"}  # остальным форматам — максимум ("0")
BROWSER_CHOICES = ("chrome", "brave", "edge", "firefox", "opera", "vivaldi")

# ---------- тёмная тема ----------

BG = "#16171c"        # фон окна
SURFACE = "#1e2026"   # панели, вкладки
FIELD = "#26282f"     # поля ввода, кнопки-призраки
FIELD_HOVER = "#2f313a"
BORDER = "#34363f"
TEXT = "#eceef2"
MUTED = "#a0a3ad"
ACCENT = "#e5484d"       # красный акцент (кнопка «Скачать», прогресс)
ACCENT_HOVER = "#f0595e"
SUCCESS = "#30c46f"
DANGER = "#ff7076"
BUSY = "#d5d8de"         # нейтральный цвет статуса «в процессе»

FONT_FAMILY = "Segoe UI"


class TorrentCancelled(Exception):
    """Загрузка торрента отменена пользователем."""


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def find_node() -> str | None:
    return shutil.which("node") or shutil.which("node.exe")


def build_opts(mode: str, outdir: str, progress_hook,
               video_quality: str = "1080p", audio_format: str = "mp3",
               cookies_browser: str | None = None,
               player_client: str | None = None) -> dict:
    """Опции yt-dlp: audio -> выбранный формат, video -> MP4 выбранного качества."""
    opts = {
        "outtmpl": os.path.join(outdir, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        # скрипты-решатели JS-челленджей YouTube (n-challenge), без них
        # современные клиенты часто отдают пустой список форматов
        "remote_components": ["ejs:github"],
    }
    # yt-dlp нужен JS-рантайм для получения ссылок YouTube;
    # deno включён по умолчанию, Node добавляем, если найден в PATH
    opts["js_runtimes"] = {"node": {}} if find_node() else {"deno": {}}
    if cookies_browser:
        # куки браузера — обход «Sign in to confirm you're not a bot»
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
    if mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
            "preferredquality": AUDIO_QUALITY.get(audio_format, "0"),
        }]
    else:
        height = VIDEO_QUALITIES.get(video_quality)
        if height:
            opts["format"] = (f"bestvideo[height<={height}]+bestaudio/"
                              f"best[height<={height}]/best")
        else:
            opts["format"] = "bestvideo*+bestaudio/best"
        opts["merge_output_format"] = "mp4"
    return opts


def fmt_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} Б"


def fmt_time(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def run_torrent(source: str, is_magnet: bool, outdir: str,
                cancel: threading.Event, report) -> str:
    """Скачать .torrent или magnet-ссылку в outdir.

    report(pct, msg) вызывается примерно раз в секунду.
    Возвращает путь к скачанному файлу/папке; при отмене бросает TorrentCancelled.
    """
    import libtorrent as lt

    ses = lt.session()
    try:
        if is_magnet:
            atp = lt.parse_magnet_uri(source)
        else:
            atp = lt.add_torrent_params()
            atp.ti = lt.torrent_info(source)
        atp.save_path = outdir
        handle = ses.add_torrent(atp)
        handle.set_flags(lt.torrent_flags.auto_managed)

        start = time.time()
        while True:
            if cancel.is_set():
                ses.remove_torrent(handle)
                raise TorrentCancelled()
            st = handle.status()
            if st.is_seeding:
                return os.path.join(outdir, st.name)
            if st.state == lt.torrent_status.downloading_metadata \
                    and time.time() - start > 240:
                raise RuntimeError(
                    "Пиры не найдены за 4 минуты: magnet «мёртв», сидов нет.")

            total, done, rate = st.total_wanted or 0, st.total_wanted_done or 0, \
                st.download_payload_rate
            name = st.name or "торрент"
            if st.state == lt.torrent_status.downloading_metadata:
                pct = 0.0
                msg = f"{name} — получение метаданных (пиры: {st.num_peers})"
            else:
                pct = done / total * 100 if total else 0.0
                msg = (f"{name} — {fmt_size(done)} из {fmt_size(total)} • "
                       f"{fmt_size(rate)}/с • пиры: {st.num_peers}")
                if rate > 0 and total:
                    msg += f" • ~{fmt_time((total - done) / rate)}"
            report(pct, msg)
            time.sleep(1)
    finally:
        del ses  # останавливает фоновые потоки сессии


# ---------- настройки (запоминаем между запусками) ----------

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def make_entry(parent, width: int, textvariable: tk.StringVar | None = None) -> tk.Entry:
    """Поле ввода в тёмной теме: плоское, с акцентной рамкой при фокусе."""
    return tk.Entry(
        parent, width=width, font=(FONT_FAMILY, 10), textvariable=textvariable,
        bg=FIELD, fg=TEXT, insertbackground=TEXT,
        relief="flat", highlightthickness=1,
        highlightbackground=BORDER, highlightcolor=ACCENT,
        selectbackground=ACCENT_HOVER, selectforeground="#ffffff",
        disabledbackground=SURFACE, disabledforeground=MUTED,
    )


class DownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False        # идёт загрузка видео/аудио
        self.tbusy = False       # идёт загрузка торрента
        self.torrent_cancel = threading.Event()
        self.settings = load_settings()

        root.title(WINDOW_TITLE)
        root.resizable(False, False)
        root.configure(background=BG)
        self._apply_theme()

        notebook = ttk.Notebook(root, style="TNotebook")
        notebook.pack(fill="both", expand=True, padx=14, pady=(10, 14))
        self.notebook = notebook
        self.media_tab = ttk.Frame(notebook, style="TFrame", padding=(14, 12))
        self.torrent_tab = ttk.Frame(notebook, style="TFrame", padding=(14, 12))
        self._build_media_tab(self.media_tab)
        self._build_torrent_tab(self.torrent_tab)
        notebook.add(self.media_tab, text="  Видео / Аудио  ")
        notebook.add(self.torrent_tab, text="  Торренты  ")

        self.root.after(100, self.poll_queue)
        self.root.after(300, self._autofill_from_clipboard)
        self.root.bind("<FocusIn>", self._on_focus_in)

    # ---------- тема ----------

    def _apply_theme(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                tkfont.nametofont(name).configure(family=FONT_FAMILY, size=10)
            except tk.TclError:
                pass

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, borderwidth=0,
                        focuscolor=BG, font=(FONT_FAMILY, 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Section.TLabel", background=BG, foreground=MUTED,
                        font=(FONT_FAMILY, 8, "bold"))
        style.configure("Status.TLabel", background=BG, foreground=MUTED)
        style.configure("Hint.TLabel", background=BG, foreground="#6f727c",
                        font=(FONT_FAMILY, 8))

        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED,
                        padding=(18, 8), borderwidth=0,
                        font=(FONT_FAMILY, 10, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", FIELD)],
                  foreground=[("selected", TEXT), ("active", TEXT)],
                  expand=[("selected", (0, 0, 0, 0))])

        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        padding=(22, 9), borderwidth=0, font=(FONT_FAMILY, 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_HOVER), ("pressed", ACCENT_HOVER)],
                  foreground=[("disabled", "#f2b8ba")])
        style.configure("Ghost.TButton", background=FIELD, foreground=TEXT,
                        padding=(14, 8), borderwidth=0)
        style.map("Ghost.TButton",
                  background=[("active", FIELD_HOVER), ("pressed", FIELD_HOVER)],
                  foreground=[("disabled", MUTED)])
        style.configure("Danger.TButton", background=FIELD, foreground=DANGER,
                        padding=(14, 8), borderwidth=0)
        style.map("Danger.TButton",
                  background=[("active", FIELD_HOVER), ("pressed", FIELD_HOVER)],
                  foreground=[("disabled", "#7c5255")])
        for name in ("Seg.TButton", "SegIdle.TButton"):
            style.configure(name, background=FIELD, foreground=MUTED,
                            padding=(16, 8), borderwidth=0)
        style.configure("Seg.TButton", background=ACCENT, foreground="#ffffff")
        style.map("Seg.TButton", background=[("active", ACCENT_HOVER)],
                  foreground=[("active", "#ffffff")])
        style.map("SegIdle.TButton",
                  background=[("active", FIELD_HOVER), ("pressed", FIELD_HOVER)],
                  foreground=[("active", TEXT)])

        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                        lightcolor=FIELD, darkcolor=FIELD, borderwidth=0,
                        padding=5)
        style.map("TCombobox",
                  fieldbackground=[("disabled", "#1a1b20"), ("readonly", FIELD)],
                  foreground=[("disabled", MUTED), ("readonly", TEXT)],
                  arrowcolor=[("disabled", MUTED), ("readonly", TEXT)],
                  background=[("active", FIELD_HOVER)])
        self.root.option_add("*TCombobox*Listbox.background", SURFACE)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", (FONT_FAMILY, 10))
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

        style.configure("TCheckbutton", background=BG, foreground=TEXT, focuscolor=BG)
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  indicatorcolor=[("selected", ACCENT), ("pressed", ACCENT)],
                  foreground=[("disabled", MUTED)])

        style.configure("Accent.Horizontal.TProgressbar",
                        background=ACCENT, troughcolor=FIELD,
                        bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT,
                        thickness=9)

    # ---------- вкладка «Видео / Аудио» ----------

    def _build_media_tab(self, main: ttk.Frame) -> None:
        main.columnconfigure(0, weight=1)
        s = self.settings

        ttk.Label(main, text="ССЫЛКА НА ВИДЕО ИЛИ АУДИО (YOUTUBE, TIKTOK, VK, INSTAGRAM, X…)",
                  style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.url_var = tk.StringVar()
        self.url_entry = make_entry(main, 68, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=0, sticky="we", pady=(5, 12), ipady=7)
        ttk.Button(main, text="Вставить", style="Ghost.TButton",
                   command=self.paste_clipboard, cursor="hand2") \
            .grid(row=1, column=1, sticky="e", padx=(10, 0), pady=(5, 12))
        self.url_entry.focus()
        self._setup_clipboard_bindings(self.url_entry)
        self.url_entry.bind("<Return>", lambda e: self.start_download())

        seg = ttk.Frame(main, style="TFrame")
        seg.grid(row=2, column=0, columnspan=2, sticky="we", pady=(0, 12))
        self.mode_var = tk.StringVar(value=s.get("mode", "audio"))
        self.seg_audio = ttk.Button(seg, text="Аудио", style="Seg.TButton",
                                    command=lambda: self._set_mode("audio"), cursor="hand2")
        self.seg_audio.pack(side="left")
        self.seg_video = ttk.Button(seg, text="Видео (MP4)", style="SegIdle.TButton",
                                    command=lambda: self._set_mode("video"), cursor="hand2")
        self.seg_video.pack(side="left", padx=(2, 18))

        qf = ttk.Frame(seg, style="TFrame")
        qf.pack(side="right")
        ttk.Label(qf, text="Качество:", style="Section.TLabel").pack(side="left")
        self.video_quality_var = tk.StringVar(value=s.get("video_quality", "1080p"))
        self.video_quality_cb = ttk.Combobox(
            qf, textvariable=self.video_quality_var, state="disabled",
            values=list(VIDEO_QUALITIES), width=11, font=(FONT_FAMILY, 10))
        self.video_quality_cb.pack(side="left", padx=(6, 16))
        ttk.Label(qf, text="Формат звука:", style="Section.TLabel").pack(side="left")
        self.audio_format_var = tk.StringVar(value=s.get("audio_format", "mp3"))
        self.audio_format_cb = ttk.Combobox(
            qf, textvariable=self.audio_format_var, state="readonly",
            values=list(AUDIO_FORMATS), width=6, font=(FONT_FAMILY, 10))
        self.audio_format_cb.pack(side="left", padx=(6, 0))

        cf = ttk.Frame(main, style="TFrame")
        cf.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 14))
        self.cookies_var = tk.BooleanVar(value=bool(s.get("cookies_on", False)))
        ttk.Checkbutton(cf, text="Куки браузера (если сайт требует вход):",
                        variable=self.cookies_var, style="TCheckbutton",
                        command=self._on_cookies_toggle, cursor="hand2").pack(side="left")
        self.cookies_browser_var = tk.StringVar(value=s.get("cookies_browser", "chrome"))
        self.cookies_browser_cb = ttk.Combobox(
            cf, textvariable=self.cookies_browser_var, state="disabled",
            values=list(BROWSER_CHOICES), width=9, font=(FONT_FAMILY, 10))
        self.cookies_browser_cb.pack(side="left", padx=(8, 0))

        ttk.Label(main, text="КУДА СОХРАНЯТЬ", style="Section.TLabel") \
            .grid(row=4, column=0, columnspan=2, sticky="w")
        self.dir_var = tk.StringVar(value=s.get("dir", DEFAULT_DIR))
        self.dir_entry = make_entry(main, 62, textvariable=self.dir_var)
        self.dir_entry.grid(row=5, column=0, sticky="we", pady=(5, 16), ipady=7)
        folder_btns = ttk.Frame(main, style="TFrame")
        folder_btns.grid(row=5, column=1, sticky="e", padx=(10, 0), pady=(5, 16))
        ttk.Button(folder_btns, text="Обзор…", style="Ghost.TButton",
                   command=lambda: self.pick_dir(self.dir_var), cursor="hand2") \
            .pack(side="left")
        ttk.Button(folder_btns, text="Открыть", style="Ghost.TButton",
                   command=lambda: self.open_folder(self.dir_var), cursor="hand2") \
            .pack(side="left", padx=(6, 0))

        drow = ttk.Frame(main, style="TFrame")
        drow.grid(row=6, column=0, columnspan=2, sticky="we")
        self.download_btn = ttk.Button(drow, text="Скачать", style="Accent.TButton",
                                       command=self.start_download, cursor="hand2")
        self.download_btn.pack(side="left")
        self.progress = ttk.Progressbar(drow, style="Accent.Horizontal.TProgressbar",
                                        maximum=100)
        self.progress.pack(side="right", fill="x", expand=True, padx=(14, 0))

        self.status_var = tk.StringVar(value="Готово к работе")
        self.status_label = ttk.Label(main, textvariable=self.status_var,
                                      style="Status.TLabel")
        self.status_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(main, style="Hint.TLabel",
                  text="Подсказка: Ctrl+V вставляет ссылку при любой раскладке клавиатуры, "
                       "Enter — начать скачивание.") \
            .grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self._set_mode(self.mode_var.get(), save=False)
        self._on_cookies_toggle()

    def _set_mode(self, mode: str, save: bool = True) -> None:
        self.mode_var.set(mode)
        if mode == "video":
            self.seg_video.configure(style="Seg.TButton")
            self.seg_audio.configure(style="SegIdle.TButton")
            self.video_quality_cb.configure(state="readonly")
            self.audio_format_cb.configure(state="disabled")
        else:
            self.seg_audio.configure(style="Seg.TButton")
            self.seg_video.configure(style="SegIdle.TButton")
            self.video_quality_cb.configure(state="disabled")
            self.audio_format_cb.configure(state="readonly")
        if save:
            self._save_current_settings()

    def _on_cookies_toggle(self) -> None:
        self.cookies_browser_cb.configure(
            state="readonly" if self.cookies_var.get() else "disabled")
        self._save_current_settings()

    def _save_current_settings(self) -> None:
        # tdir_var появляется позже dir_var (вторая вкладка) — не падаем при раннем вызове
        self.settings.update({
            "mode": self.mode_var.get(),
            "video_quality": self.video_quality_var.get(),
            "audio_format": self.audio_format_var.get(),
            "cookies_on": bool(self.cookies_var.get()),
            "cookies_browser": self.cookies_browser_var.get(),
            "dir": self.dir_var.get() if hasattr(self, "dir_var") else self.settings.get("dir", DEFAULT_DIR),
            "tdir": self.tdir_var.get() if hasattr(self, "tdir_var") else self.settings.get("tdir", DEFAULT_DIR),
        })
        save_settings(self.settings)

    def start_download(self) -> None:
        if self.busy:
            return
        url = self.url_var.get().strip()
        if not url:
            self._set_status(self.status_var, self.status_label,
                             "Вставьте ссылку на видео.", "err")
            messagebox.showwarning(WINDOW_TITLE, "Вставьте ссылку на видео.")
            return
        outdir = self.dir_var.get().strip() or DEFAULT_DIR
        if not find_ffmpeg():
            messagebox.showerror(
                WINDOW_TITLE,
                "ffmpeg не найден в PATH.\n"
                "Установите ffmpeg (https://www.gyan.dev/ffmpeg/builds/) —\n"
                "он нужен для конвертации аудио и склейки видео.")
            return

        self.busy = True
        self._save_current_settings()
        self.progress["value"] = 0
        self._set_status(self.status_var, self.status_label, "Подключение…", "busy")
        self.download_btn.state(["disabled"])
        threading.Thread(
            target=self.worker,
            args=(url, self.mode_var.get(), outdir,
                  self.video_quality_var.get(), self.audio_format_var.get(),
                  self.cookies_browser_var.get() if self.cookies_var.get() else None),
            daemon=True).start()

    def worker(self, url: str, mode: str, outdir: str,
               video_quality: str, audio_format: str, cookies_browser: str | None) -> None:
        opts = build_opts(mode, outdir, self.hook_progress, video_quality,
                          audio_format, cookies_browser)

        try:
            paths = self._download(url, opts)
            self.q.put(("done", paths[0] if paths else outdir))
        except Exception as exc:
            msg = str(exc).strip().splitlines()[-1][:400]
            low = msg.lower()
            if "sign in to confirm" in low or "not a bot" in low:
                # бот-проверка YouTube — пробуем встроенный плеер (обход для Shorts)
                self.q.put(("progress", 0.0, "Бот-проверка YouTube — пробую обходной клиент…"))
                fallback = build_opts(mode, outdir, self.hook_progress, video_quality,
                                      audio_format, cookies_browser,
                                      player_client="web_embedded")
                try:
                    paths = self._download(url, fallback)
                    self.q.put(("done", paths[0] if paths else outdir))
                    return
                except Exception as exc2:
                    msg = str(exc2).strip().splitlines()[-1][:400]
                    msg += ("\n\nНе помогло. Включите «Куки браузера», выберите свой "
                            "браузер, ЗАКРОЙТЕ его и повторите.")
            elif "cookies" in low:
                msg += ("\n\nПодсказка: включите «Куки браузера», выберите свой браузер, "
                        "ЗАКРОЙТЕ его и повторите.")
            self.q.put(("error", msg))

    def _download(self, url: str, opts: dict) -> list[str]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        return self.collect_files(info)

    @staticmethod
    def collect_files(info: dict) -> list[str]:
        """Пути к готовым файлам после всех постпроцессоров (mp3/mp4)."""
        out = [rd.get("filepath") for rd in (info.get("requested_downloads") or [])]
        for entry in (info.get("entries") or []):
            out += DownloaderApp.collect_files(entry)
        return [p for p in out if p]

    def hook_progress(self, d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = done / total * 100 if total else 0
            speed = (d.get("_speed_str") or "?").strip()
            eta = (d.get("_eta_str") or "?").strip()
            self.q.put(("progress", pct, f"Скачивание: {pct:.0f}%   {speed}   осталось {eta}"))
        elif d["status"] == "finished":
            self.q.put(("progress", 100.0, "Обработка (конвертация/склейка)…"))

    # ---------- вкладка «Торренты» ----------

    def _build_torrent_tab(self, main: ttk.Frame) -> None:
        main.columnconfigure(0, weight=1)
        s = self.settings

        ttk.Label(main, text="MAGNET-ССЫЛКА ИЛИ ПУТЬ К .TORRENT-ФАЙЛУ",
                  style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.torrent_var = tk.StringVar()
        self.torrent_entry = make_entry(main, 68, textvariable=self.torrent_var)
        self.torrent_entry.grid(row=1, column=0, sticky="we", pady=(5, 16), ipady=7)
        ttk.Button(main, text="Выбрать .torrent…", style="Ghost.TButton",
                   command=self.pick_torrent_file, cursor="hand2") \
            .grid(row=1, column=1, sticky="e", padx=(10, 0), pady=(5, 16))
        self._setup_clipboard_bindings(self.torrent_entry)
        self.torrent_entry.bind("<Return>", lambda e: self.start_torrent())

        ttk.Label(main, text="КУДА СОХРАНЯТЬ", style="Section.TLabel") \
            .grid(row=2, column=0, columnspan=2, sticky="w")
        self.tdir_var = tk.StringVar(value=s.get("tdir", DEFAULT_DIR))
        self.tdir_entry = make_entry(main, 62, textvariable=self.tdir_var)
        self.tdir_entry.grid(row=3, column=0, sticky="we", pady=(5, 16), ipady=7)
        tbtns = ttk.Frame(main, style="TFrame")
        tbtns.grid(row=3, column=1, sticky="e", padx=(10, 0), pady=(5, 16))
        ttk.Button(tbtns, text="Обзор…", style="Ghost.TButton",
                   command=lambda: self.pick_dir(self.tdir_var), cursor="hand2") \
            .pack(side="left")
        ttk.Button(tbtns, text="Открыть", style="Ghost.TButton",
                   command=lambda: self.open_folder(self.tdir_var), cursor="hand2") \
            .pack(side="left", padx=(6, 0))

        trow = ttk.Frame(main, style="TFrame")
        trow.grid(row=4, column=0, columnspan=2, sticky="we")
        self.tbtn = ttk.Button(trow, text="Скачать", style="Accent.TButton",
                               command=self.start_torrent, cursor="hand2")
        self.tbtn.pack(side="left")
        self.tcancel_btn = ttk.Button(trow, text="Отмена", style="Danger.TButton",
                                      command=self.cancel_torrent, state="disabled",
                                      cursor="hand2")
        self.tcancel_btn.pack(side="left", padx=(8, 0))
        self.tprogress = ttk.Progressbar(trow, style="Accent.Horizontal.TProgressbar",
                                         length=400, maximum=100)
        self.tprogress.pack(side="right", fill="x", expand=True)

        self.tstatus_var = tk.StringVar(value="Готово к работе")
        self.tstatus_label = ttk.Label(main, textvariable=self.tstatus_var,
                                       style="Status.TLabel")
        self.tstatus_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(main, style="Hint.TLabel",
                  text="Подсказка: скорость торрента зависит от числа сидов; "
                       " magnet-ссылка из буфера подставится сама.") \
            .grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def pick_torrent_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите .torrent-файл",
            filetypes=[("Торрент-файлы", "*.torrent"), ("Все файлы", "*.*")])
        if path:
            self.torrent_var.set(os.path.normpath(path))

    def start_torrent(self) -> None:
        if self.tbusy:
            return
        src = self.torrent_var.get().strip().strip('"')
        if not src:
            self._set_status(self.tstatus_var, self.tstatus_label,
                             "Укажите magnet-ссылку или .torrent-файл.", "err")
            messagebox.showwarning(WINDOW_TITLE,
                                   "Укажите magnet-ссылку или выберите .torrent-файл.")
            return
        is_magnet = src.lower().startswith("magnet:")
        if not is_magnet and not os.path.isfile(src):
            messagebox.showerror(WINDOW_TITLE, f"Файл не найден:\n{src}")
            return
        outdir = self.tdir_var.get().strip() or DEFAULT_DIR

        self.tbusy = True
        self._save_current_settings()
        self.torrent_cancel.clear()
        self.tprogress["value"] = 0
        self._set_status(self.tstatus_var, self.tstatus_label, "Подключение…", "busy")
        self.tbtn.state(["disabled"])
        self.tcancel_btn.state(["!disabled"])
        threading.Thread(target=self.torrent_worker, args=(src, is_magnet, outdir),
                         daemon=True).start()

    def cancel_torrent(self) -> None:
        self.torrent_cancel.set()
        self._set_status(self.tstatus_var, self.tstatus_label, "Останавливаю…", "busy")

    def torrent_worker(self, source: str, is_magnet: bool, outdir: str) -> None:
        def report(pct: float, msg: str) -> None:
            self.q.put(("tprogress", pct, msg))

        try:
            path = run_torrent(source, is_magnet, outdir, self.torrent_cancel, report)
            self.q.put(("tdone", path))
        except TorrentCancelled:
            self.q.put(("tcancel",))
        except ImportError:
            self.q.put(("terror",
                        "Не установлена библиотека libtorrent.\n"
                        "Установите её командой:  pip install libtorrent"))
        except Exception as exc:
            self.q.put(("terror", str(exc)[:400]))

    # ---------- общее ----------

    @staticmethod
    def _set_status(var: tk.StringVar, label: ttk.Label, text: str, kind: str) -> None:
        var.set(text)
        label.configure(foreground={"ok": SUCCESS, "err": DANGER,
                                    "busy": BUSY}.get(kind, MUTED))

    def pick_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(initialdir=var.get() or DEFAULT_DIR)
        if path:
            var.set(os.path.normpath(path))
            self._save_current_settings()

    def open_folder(self, var: tk.StringVar) -> None:
        path = var.get().strip() or DEFAULT_DIR
        if os.path.isdir(path):
            os.startfile(path)  # Windows

    # ---------- буфер обмена: вставка в любом виде ----------

    def _setup_clipboard_bindings(self, entry) -> None:
        # Ctrl+V/C/X/A по коду клавиши — работает и на русской раскладке,
        # где стандартные привязки tkinter (по букве) не срабатывают
        entry.bind("<Control-KeyPress>", self._on_ctrl_key)
        entry.bind("<Shift-Insert>", lambda e: self.paste_clipboard(e, replace_all=False))
        entry.bind("<Control-Insert>", self.copy_clipboard)   # Ctrl+Ins
        # ПКМ — контекстное меню
        entry.bind("<Button-3>", self._show_context_menu)

    def _on_ctrl_key(self, event) -> str | None:
        if event.keycode == 86:    # V
            return self.paste_clipboard(event, replace_all=False)
        if event.keycode == 67:    # C
            return self.copy_clipboard()
        if event.keycode == 88:    # X
            return self.cut_clipboard()
        if event.keycode == 65:    # A
            entry = self.root.focus_get()
            if isinstance(entry, (ttk.Entry, tk.Entry)):
                entry.select_range(0, "end")
                entry.icursor("end")
            return "break"
        return None

    def _clipboard_text(self) -> str:
        try:
            return self.root.clipboard_get().strip()
        except tk.TclError:
            return ""

    def paste_clipboard(self, event=None, replace_all: bool = True) -> str:
        """Кнопка/меню (replace_all=True) заменяют содержимое поля целиком;
        Ctrl+V / Shift+Insert работают как обычно: курсор или выделение."""
        entry = self.root.focus_get()
        if not isinstance(entry, (ttk.Entry, tk.Entry)):
            entry = self.url_entry
        text = self._clipboard_text()
        if text:
            if replace_all:
                entry.delete(0, "end")
                entry.insert(0, text)
            else:
                try:  # заменить выделенное, если оно есть
                    entry.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass
                entry.insert("insert", text)
            entry.icursor("end")
        return "break"

    def copy_clipboard(self, event=None) -> str:
        entry = self.root.focus_get()
        if not isinstance(entry, (ttk.Entry, tk.Entry)):
            return "break"
        try:  # у ttk.Entry.get() нет индексов — берём выделение через index()
            i = entry.index("sel.first")
            j = entry.index("sel.last")
        except tk.TclError:
            return "break"
        self.root.clipboard_clear()
        self.root.clipboard_append(entry.get()[i:j])
        return "break"

    def cut_clipboard(self, event=None) -> str:
        self.copy_clipboard()
        entry = self.root.focus_get()
        if isinstance(entry, (ttk.Entry, tk.Entry)):
            try:
                entry.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
        return "break"

    def _build_context_menu(self, entry=None) -> tk.Menu:
        entry = entry if entry is not None else self.root.focus_get()
        if not isinstance(entry, (ttk.Entry, tk.Entry)):
            entry = self.url_entry
        menu = tk.Menu(self.root, tearoff=0, bg=SURFACE, fg=TEXT,
                       activebackground=FIELD_HOVER, activeforeground=TEXT,
                       bd=0)
        has_sel = entry.selection_present()
        menu.add_command(label="Копировать", command=self.copy_clipboard,
                         state="normal" if has_sel else "disabled")
        menu.add_command(label="Вставить", command=self.paste_clipboard,
                         state="normal" if self._clipboard_text() else "disabled")
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: (
            entry.select_range(0, "end"), entry.icursor("end")))
        return menu

    def _show_context_menu(self, event) -> None:
        event.widget.focus_set()
        self._build_context_menu(event.widget).tk_popup(event.x_root, event.y_root)

    def _autofill_from_clipboard(self) -> None:
        """Если поле пустое, а в буфере ссылка — подставить её."""
        if self.busy or self.url_var.get().strip():
            return
        text = self._clipboard_text()
        if text.lower().startswith(("http://", "https://", "magnet:")):
            self.url_var.set(text)
            self.url_entry.icursor("end")

    def _on_focus_in(self, event) -> None:
        # только когда окно получило фокус извне; внутренние переключения
        # вкладок тоже кидают FocusIn — по ним поле не трогаем
        if event.widget is self.root:
            self._autofill_from_clipboard()

    # ---------- очередь сообщений из потоков в GUI ----------

    def poll_queue(self) -> None:
        try:
            while True:
                kind, *payload = self.q.get_nowait()
                if kind == "progress":
                    pct, text = payload
                    self.progress["value"] = pct
                    self._set_status(self.status_var, self.status_label, text, "busy")
                elif kind == "done":
                    self.progress["value"] = 100
                    self._set_status(self.status_var, self.status_label,
                                     f"Готово: {os.path.basename(str(payload[0]))}", "ok")
                    self.busy = False
                    self.download_btn.state(["!disabled"])
                    messagebox.showinfo(WINDOW_TITLE, f"Файл сохранён:\n{payload[0]}")
                elif kind == "error":
                    self._set_status(self.status_var, self.status_label,
                                     "Ошибка — подробности в окне сообщения.", "err")
                    self.busy = False
                    self.download_btn.state(["!disabled"])
                    messagebox.showerror(WINDOW_TITLE, f"Ошибка загрузки:\n{payload[0]}")
                elif kind == "tprogress":
                    pct, text = payload
                    self.tprogress["value"] = pct
                    self._set_status(self.tstatus_var, self.tstatus_label, text, "busy")
                elif kind == "tdone":
                    self.tprogress["value"] = 100
                    self._set_status(self.tstatus_var, self.tstatus_label,
                                     f"Готово: {os.path.basename(str(payload[0]))}", "ok")
                    self._torrent_finished()
                    messagebox.showinfo(WINDOW_TITLE, f"Скачано:\n{payload[0]}")
                elif kind == "tcancel":
                    self._set_status(self.tstatus_var, self.tstatus_label,
                                     "Загрузка отменена (частичные файлы сохранены).", "err")
                    self._torrent_finished()
                elif kind == "terror":
                    self._set_status(self.tstatus_var, self.tstatus_label,
                                     "Ошибка — подробности в окне сообщения.", "err")
                    self._torrent_finished()
                    messagebox.showerror(WINDOW_TITLE, f"Ошибка загрузки торрента:\n{payload[0]}")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def _torrent_finished(self) -> None:
        self.tbusy = False
        self.tbtn.state(["!disabled"])
        self.tcancel_btn.state(["disabled"])


def _log_crash(text: str) -> None:
    """Под pythonw консоли нет — ошибки пишем в error.log рядом с программой."""
    try:
        path = os.path.join(APP_DIR, "error.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{text}\n\n")
    except OSError:
        pass


def finalize_window(root: tk.Tk) -> None:
    """Задать окну фиксированную ширину (поля растянутся) и отцентровать."""
    root.update_idletasks()
    w = max(1120, root.winfo_reqwidth() + 40)
    h = root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")


def main() -> None:
    root = None
    try:
        root = tk.Tk()
        root.report_callback_exception = lambda t, v, tb: _log_crash(
            "".join(traceback.format_exception(t, v, tb)))
        DownloaderApp(root)
        finalize_window(root)
        # вывести поверх всех, затем отпустить
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.after(400, lambda: root.attributes("-topmost", False))
        root.mainloop()
    except Exception:
        _log_crash(traceback.format_exc())
        if root is not None:
            root.destroy()


if __name__ == "__main__":
    main()
