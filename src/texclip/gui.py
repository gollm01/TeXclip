"""TeXclip GUI: type LaTeX math, see it rendered, copy it as a vector object for Affinity.

Built with customtkinter (light / dark / system theme). tkinterdnd2 adds dropping a preamble
file onto the preamble icon; without it, clicking the icon still opens a file dialog.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover - friendly message for a double-click start
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("TeXclip", "TeXclip needs customtkinter:\n\n    pip install customtkinter tkinterdnd2")
    raise SystemExit(1)

from . import __version__
from .core import (ALIGNMENTS, FONTS, FROM_PREAMBLE, OUTDIR, PREAMBLE_FILE, Options, baseline_shift,
                   build_preamble, build_result, install_preamble, missing_tools, normalize_color,
                   read_user_preamble, render_preview, tex_dim, text_overflow,
                   strip_overrides, to_clipboard)
from .shortcut import APP_ID, ICON_ICO, ICON_PNG, set_window_identity

try:  # optional: drop files onto the preamble icon
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DnD = TkinterDnD.DnDWrapper
except Exception:  # pragma: no cover
    DND_FILES, TkinterDnD, _DnD = None, None, object

CFG_FILE = os.path.join(os.path.expanduser("~"), ".texclip_gui.json")
FONT_LABELS = {k: f"{k} - {d}" for k, (_, d) in FONTS.items()}
KEY_FROM_LABEL = {v: k for k, v in FONT_LABELS.items()}
PASTE_LABELS = {"object": "One object (the whole equation is a single curve)",
                "curves": "Curves (one curve per glyph, editable)"}
PASTE_FROM_LABEL = {v: k for k, v in PASTE_LABELS.items()}
ANCHOR_LABELS = {"baseline": "Baseline (TeX baseline, shows the shift)",
                 "ink": "Ink (lowest written character)",
                 "baseline-crop": "Baseline, cropped (experimental)"}
ANCHOR_FROM_LABEL = {v: k for k, v in ANCHOR_LABELS.items()}
ALIGN_LABELS = {k: v[0] for k, v in ALIGNMENTS.items()}
ALIGN_FROM_LABEL = {v: k for k, v in ALIGN_LABELS.items()}
MODES = {"math": "Equation", "text": "Text block"}
UNITS = ["mm", "pt", "cm", "in"]
DASH, MINUS, ARROW = "\u2013", "\u2212", "\u2192"
ZOOMS = ["100 %", "150 %", "200 %", "300 %", "400 %"]
APPEARANCES = ["Light", "Dark", "System"]
MONO = ("Consolas", 14) if sys.platform == "win32" else ("Menlo", 13)
PREAMBLE_STUB = ("% TeXclip personal preamble - inserted after the base packages.\n"
                 "% Font, size and colour come from the TeXclip window unless you tick 'preamble'\n"
                 "% next to them; then the settings made here are used.\n"
                 "% Example:\n% \\newcommand{\\Hamil}{\\hat{H}}\n")
DEFAULTS = dict(size="10", color="000000", font="cm", size_pre=False, color_pre=False, font_pre=False,
                anchor="baseline", mode="math", text="", tb_width="80", tb_height="", tb_unit="mm",
                align="justify-left", leading="1.2", parskip="0", indent="0", hyphenate=True,
                bold=False, inline=False, border="0.5", paste="object", zoom="200 %", topmost=False,
                appearance="System", history=[], last_dir="",
                eq=r"i\hbar\,\partial_t\ket{\psi}=\hat H\ket{\psi}", geometry="1180x760")

# TeXclip palette (light, dark) - from the icon
TEAL = ("#0f4c5c", "#17687d")
TEAL_HOVER = ("#003049", "#1f8099")
ORANGE, ORANGE_HOVER = "#fb8b24", "#e07612"
RED = ("#c0392b", "#ff6b5b")
MUTED = ("gray40", "gray65")


def _theme() -> None:
    """Recolour customtkinter's blue theme with the TeXclip teal."""
    ctk.set_default_color_theme("blue")
    t = ctk.ThemeManager.theme
    for widget, keys in {"CTkButton": ("fg_color",), "CTkCheckBox": ("fg_color",),
                         "CTkSwitch": ("progress_color",), "CTkSegmentedButton": ("selected_color",),
                         "CTkOptionMenu": ("fg_color",), "CTkSlider": ("button_color", "progress_color")}.items():
        for k in keys:
            if k in t.get(widget, {}):
                t[widget][k] = list(TEAL)
    for widget, k in (("CTkButton", "hover_color"), ("CTkCheckBox", "hover_color"),
                      ("CTkSegmentedButton", "selected_hover_color"), ("CTkOptionMenu", "button_color")):
        if k in t.get(widget, {}):
            t[widget][k] = list(TEAL_HOVER)
    if "CTkOptionMenu" in t:
        t["CTkOptionMenu"]["button_hover_color"] = ["#002536", "#2391ad"]


def load_cfg() -> dict:
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def luminance(hex6: str) -> float:
    r, g, b = (int(hex6[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def open_path(p) -> None:
    if sys.platform == "win32":
        os.startfile(p)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])


class App(ctk.CTk, _DnD):
    PAD = 16

    def __init__(self) -> None:
        super().__init__()
        self.withdraw()                               # shown once the taskbar identity is set
        self.dnd = False
        if TkinterDnD is not None:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd = True
            except Exception:  # noqa: BLE001 - tkdnd binary missing for this platform
                pass
        self.cfg = load_cfg()
        ctk.set_appearance_mode(self.cfg["appearance"])
        self.title(f"TeXclip {__version__}")
        self._set_icon()
        self.geometry(self.cfg["geometry"])
        self.minsize(900, 600)
        self.q: queue.Queue = queue.Queue()
        self.gen = 0                     # id of the newest preview request; older results are dropped
        self._after = None
        self._img = None
        self._error = ""
        self._busy = 0
        self._pre_mtime = self._mtime()
        self._mode = ctk.get_appearance_mode()
        self.preamble_text = ""

        self._build_ui()
        self._load_into_ui()
        self.update_idletasks()
        if sys.platform == "win32":                   # own taskbar button with the TeXclip icon
            try:
                set_window_identity(int(self.wm_frame(), 16))
            except (ValueError, tk.TclError):
                pass
        # let customtkinter show the window as usual (after it has set the title bar colour)
        self._withdraw_called_before_window_exists = False
        self._poll_id = self.after(60, self._poll)
        self.bind("<FocusIn>", self._on_focus)
        self.protocol("WM_DELETE_WINDOW", self._close)
        if missing_tools(("latex", "dvisvgm")):
            messagebox.showwarning("TeX not found", "latex / dvisvgm were not found on PATH.\n"
                                   "Install MiKTeX or TeX Live and restart TeXclip.")
        self.update_preamble()
        self.refresh()

    def _set_icon(self) -> None:
        """TeXclip icon in the title bar and Alt+Tab (replaces the default icon)."""
        try:
            if sys.platform == "win32":
                self.iconbitmap(default=str(ICON_ICO))      # also for dialogs
                self.iconbitmap(str(ICON_ICO))              # tells customtkinter not to set its own
            else:
                self._icon_img = tk.PhotoImage(file=str(ICON_PNG))
                self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ UI construction
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=2, uniform="cols")
        self.grid_columnconfigure(1, weight=3, uniform="cols")
        self.grid_rowconfigure(1, weight=1)
        bold = ctk.CTkFont(size=14, weight="bold")

        # ---- top bar
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(12, 6))
        self.icon = tk.Canvas(bar, width=46, height=56, highlightthickness=0, cursor="hand2")
        self.icon.pack(side="left")
        self._icon_hover = False
        self.icon.bind("<Enter>", lambda e: self._hover_icon(True))
        self.icon.bind("<Leave>", lambda e: self._hover_icon(False))
        self.icon.bind("<ButtonRelease-1>", lambda e: self.load_preamble())
        if self.dnd:
            self.icon.drop_target_register(DND_FILES)
            self.icon.dnd_bind("<<DropEnter>>", lambda e: (self._hover_icon(True), e.action)[1])
            self.icon.dnd_bind("<<DropLeave>>", lambda e: (self._hover_icon(False), e.action)[1])
            self.icon.dnd_bind("<<Drop>>", self._on_drop)
        txt = ctk.CTkFrame(bar, fg_color="transparent")
        txt.pack(side="left", padx=10)
        ctk.CTkLabel(txt, text="Preamble", font=bold, height=20).pack(anchor="w")
        self.icon_hint_text = ("click to load a preamble file, or drop one here" if self.dnd
                               else "click to load a preamble file")
        self.icon_hint = ctk.CTkLabel(txt, text=self.icon_hint_text, text_color=MUTED, height=18)
        self.icon_hint.pack(anchor="w")

        self.v_app = tk.StringVar()
        ctk.CTkSegmentedButton(bar, values=APPEARANCES, variable=self.v_app,
                               command=self._set_appearance).pack(side="right")
        self.v_top = tk.BooleanVar()
        ctk.CTkSwitch(bar, text="Always on top", variable=self.v_top,
                      command=lambda: self.attributes("-topmost", self.v_top.get())).pack(side="right", padx=16)
        self.v_hist = tk.StringVar(value="History")
        self.hist = ctk.CTkOptionMenu(bar, variable=self.v_hist, values=["(empty)"], width=300,
                                      dynamic_resizing=False, command=self._pick_history,
                                      fg_color=("gray80", "gray25"), button_color=("gray72", "gray30"),
                                      button_hover_color=("gray64", "gray36"), text_color=("gray10", "gray90"))
        self.hist.pack(side="right")

        # ---- left column
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=(14, 7))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)

        ef = ctk.CTkFrame(left)
        ef.grid(row=0, column=0, sticky="nsew")
        ef.grid_columnconfigure(0, weight=1)
        ef.grid_rowconfigure(1, weight=1)
        self.v_mode = tk.StringVar(value=MODES["math"])
        self.mode = "math"
        ctk.CTkSegmentedButton(ef, values=list(MODES.values()), variable=self.v_mode,
                               command=self._on_mode).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))
        self.eq_hint = ctk.CTkLabel(ef, text="", text_color=MUTED)
        self.eq_hint.grid(row=0, column=0, sticky="e", padx=12, pady=(8, 0))
        self.eq = ctk.CTkTextbox(ef, font=MONO, undo=True, wrap="none", border_width=1)
        self.eq.grid(row=1, column=0, sticky="nsew", padx=10, pady=(4, 10))
        self.eq.bind("<<Modified>>", self._on_modified)
        self.eq.bind("<Control-Return>", lambda e: (self.copy(), "break")[1])
        self.bind("<F5>", lambda e: self.refresh())

        sf = ctk.CTkFrame(left)
        sf.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        sf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(sf, text="Style", font=bold).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        ctk.CTkLabel(sf, text="these fields override your preamble file",
                     text_color=MUTED).grid(row=0, column=1, columnspan=2, sticky="e", padx=12, pady=(8, 2))
        self.v_size, self.v_color, self.v_font = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.v_border, self.v_paste = tk.StringVar(), tk.StringVar()
        self.v_bold, self.v_inline = tk.BooleanVar(), tk.BooleanVar()
        self.v_size_pre, self.v_color_pre, self.v_font_pre = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()

        def row_label(r, text):
            ctk.CTkLabel(sf, text=text).grid(row=r, column=0, sticky="w", padx=12, pady=4)

        def pre_box(r, var):
            ctk.CTkCheckBox(sf, text="from preamble", variable=var, checkbox_width=20, checkbox_height=20,
                            command=self._on_pre_toggle).grid(row=r, column=2, sticky="w", padx=(8, 12))

        row_label(1, "Font size")
        r = ctk.CTkFrame(sf, fg_color="transparent")
        r.grid(row=1, column=1, sticky="w")
        self.e_size = ctk.CTkEntry(r, width=70, textvariable=self.v_size)
        self.e_size.pack(side="left")
        ctk.CTkLabel(r, text="pt").pack(side="left", padx=6)
        pre_box(1, self.v_size_pre)

        row_label(2, "Colour")
        r = ctk.CTkFrame(sf, fg_color="transparent")
        r.grid(row=2, column=1, sticky="w")
        ctk.CTkLabel(r, text="#").pack(side="left", padx=(0, 3))
        self.e_color = ctk.CTkEntry(r, width=90, textvariable=self.v_color)
        self.e_color.pack(side="left")
        self.swatch = ctk.CTkButton(r, text="", width=30, height=26, border_width=1, hover=False,
                                    border_color=("gray50", "gray60"), command=self._pick_color)
        self.swatch.pack(side="left", padx=8)
        self.b_pick = ctk.CTkButton(r, text="Pick…", width=70, command=self._pick_color)
        self.b_pick.pack(side="left")
        pre_box(2, self.v_color_pre)

        row_label(3, "Font")
        self.o_font = ctk.CTkOptionMenu(sf, variable=self.v_font, values=list(FONT_LABELS.values()),
                                        dynamic_resizing=False, command=lambda _: self._on_style())
        self.o_font.grid(row=3, column=1, sticky="ew")
        pre_box(3, self.v_font_pre)

        row_label(4, "Options")
        r = ctk.CTkFrame(sf, fg_color="transparent")
        r.grid(row=4, column=1, columnspan=2, sticky="w")
        ctk.CTkCheckBox(r, text="Bold", variable=self.v_bold, width=70, command=self._on_style).pack(side="left")
        self.cb_inline = ctk.CTkCheckBox(r, text="Inline style", variable=self.v_inline, width=110,
                                         command=self._on_style)
        self.cb_inline.pack(side="left", padx=(8, 14))
        ctk.CTkLabel(r, text="Border").pack(side="left", padx=(0, 6))
        self.e_border = ctk.CTkEntry(r, width=56, textvariable=self.v_border)
        self.e_border.pack(side="left")
        ctk.CTkLabel(r, text="pt").pack(side="left", padx=6)

        row_label(5, "Paste as")
        ctk.CTkOptionMenu(sf, variable=self.v_paste, values=list(PASTE_LABELS.values()),
                          dynamic_resizing=False).grid(row=5, column=1, columnspan=2, sticky="ew",
                                                       padx=(0, 12), pady=4)
        row_label(6, "Anchor")
        self.v_anchor = tk.StringVar()
        self.o_anchor = ctk.CTkOptionMenu(sf, variable=self.v_anchor, values=list(ANCHOR_LABELS.values()),
                                          dynamic_resizing=False, command=lambda _: self._on_style())
        self.o_anchor.grid(row=6, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=(4, 12))
        for v in (self.v_size, self.v_color, self.v_border):
            v.trace_add("write", lambda *_: self._on_style())

        self._build_text_block(left, bold)

        ab = ctk.CTkFrame(left, fg_color="transparent")
        ab.grid(row=3, column=0, sticky="ew", pady=10)
        ab.grid_columnconfigure(0, weight=1)
        self.btn_copy = ctk.CTkButton(ab, text="Copy to clipboard", height=40, fg_color=ORANGE,
                                      hover_color=ORANGE_HOVER, text_color="#1f1300",
                                      font=ctk.CTkFont(size=15, weight="bold"), command=self.copy)
        self.btn_copy.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(ab, text="Output folder", width=120, height=40,
                      command=lambda: (OUTDIR.mkdir(parents=True, exist_ok=True), open_path(OUTDIR))
                      ).grid(row=0, column=1, padx=(8, 0))

        # ---- right column: tabs
        tabs = ctk.CTkTabview(self, anchor="nw")
        tabs.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=(0, 10))
        pv, pt, lg = tabs.add("Preview"), tabs.add("Preamble"), tabs.add("Log")

        tb = ctk.CTkFrame(pv, fg_color="transparent")
        tb.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(tb, text="Zoom").pack(side="left")
        self.v_zoom = tk.StringVar()
        ctk.CTkOptionMenu(tb, variable=self.v_zoom, values=ZOOMS, width=90,
                          command=lambda _: self.refresh()).pack(side="left", padx=8)
        ctk.CTkLabel(tb, text="preview only - the exported vector is size-independent",
                     text_color=MUTED).pack(side="left")
        wrap = ctk.CTkFrame(pv, corner_radius=8)
        wrap.pack(fill="both", expand=True)
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg="white")
        xs = ctk.CTkScrollbar(wrap, orientation="horizontal", command=self.canvas.xview)
        ys = ctk.CTkScrollbar(wrap, orientation="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xs.set, yscrollcommand=ys.set)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=(6, 0))
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.canvas.bind("<Configure>", lambda e: self._draw())

        sh = ctk.CTkFrame(pv, fg_color="transparent")
        sh.pack(fill="x", pady=(8, 0))
        self.shift_label = ctk.CTkLabel(sh, text=f"Baseline shift: {DASH}",
                                        font=ctk.CTkFont(size=14, weight="bold"))
        self.shift_label.pack(side="left")
        self.b_shift = ctk.CTkButton(sh, text="Copy value", width=110, command=self.copy_shift)
        self.b_shift.pack(side="left", padx=12)
        self.shift_hint = ctk.CTkLabel(
            pv, text=f"Paste the equation first, then copy this value into Character panel {ARROW} "
                     "Baseline shift. Valid when the equation is pasted at 100 %.",
            text_color=MUTED, font=ctk.CTkFont(size=11), anchor="w", justify="left")
        self.shift_hint.pack(fill="x")
        self.shift = None

        pb = ctk.CTkFrame(pt, fg_color="transparent")
        pb.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(pb, text="Copy preamble", width=130, command=self.copy_preamble).pack(side="left")
        ctk.CTkButton(pb, text="Load file…", width=100, command=self.load_preamble).pack(side="left", padx=6)
        ctk.CTkButton(pb, text="Edit file", width=90, command=self.edit_preamble_file).pack(side="left")
        ctk.CTkButton(pb, text="Open folder", width=100,
                      command=lambda: open_path(PREAMBLE_FILE.parent)).pack(side="left", padx=6)
        self.pre_txt = ctk.CTkTextbox(pt, font=MONO, wrap="none", state="disabled")
        self.pre_txt.pack(fill="both", expand=True)
        self.pre_note = ctk.CTkLabel(pt, text="", text_color=MUTED, justify="left", anchor="w", wraplength=560)
        self.pre_note.pack(fill="x", pady=(6, 0))

        self.log = ctk.CTkTextbox(lg, font=MONO, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

        self.status = ctk.CTkLabel(self, text="Ready", anchor="w", height=26, corner_radius=0,
                                   fg_color=("gray84", "gray18"), padx=14)
        self.status.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._recolor()

    # ------------------------------------------------------------------ preamble icon
    def _hover_icon(self, on: bool) -> None:
        self._icon_hover = on
        self._draw_icon()

    def _draw_icon(self) -> None:
        dark = ctk.get_appearance_mode() == "Dark"
        c = self.icon
        c.configure(bg=self._color(ctk.ThemeManager.theme["CTk"]["fg_color"]))
        c.delete("all")
        if self._icon_hover:
            fill, edge = ORANGE, "#9a031e"
        else:
            fill, edge = ("#2a3338", "#7fb3c2") if dark else ("#ffffff", "#0f4c5c")
        c.create_polygon(6, 4, 32, 4, 42, 14, 42, 52, 6, 52, fill=fill, outline=edge, width=2)
        c.create_polygon(32, 4, 32, 14, 42, 14, fill="#d9d9d9" if not dark else "#55636a", outline=edge, width=2)
        for i, w in enumerate((26, 20, 26, 14)):
            c.create_line(12, 24 + 7 * i, 12 + w, 24 + 7 * i, fill=edge, width=2)
        c.create_text(15, 16, text="\\", font=("Consolas", 12, "bold"), fill=edge)

    def _color(self, pair) -> str:
        if isinstance(pair, (list, tuple)):
            return pair[1] if ctk.get_appearance_mode() == "Dark" else pair[0]
        return pair

    def _recolor(self) -> None:
        """Plain tk widgets (and the Windows title bar) do not follow the theme by themselves."""
        self._draw_icon()
        self._draw()
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = int(self.wm_frame(), 16)
                on = ctypes.c_int(1 if ctk.get_appearance_mode() == "Dark" else 0)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)  # immersive dark
                ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x27)   # redraw the frame
            except (OSError, ValueError, AttributeError, tk.TclError):
                pass

    def _set_appearance(self, mode: str) -> None:
        ctk.set_appearance_mode(mode)
        self.after(50, self._recolor)

    # ------------------------------------------------------------------ state <-> UI
    def _load_into_ui(self) -> None:
        c = self.cfg
        self.v_size.set(str(c["size"]) if not str(c["size"]) == FROM_PREAMBLE else "10")
        self.v_color.set(c["color"])
        self.v_font.set(FONT_LABELS.get(c["font"], FONT_LABELS["cm"]))
        self.v_size_pre.set(bool(c["size_pre"]))
        self.v_color_pre.set(bool(c["color_pre"]))
        self.v_font_pre.set(bool(c["font_pre"]))
        self.v_bold.set(bool(c["bold"]))
        self.v_inline.set(bool(c["inline"]))
        self.v_border.set(str(c["border"]))
        self.v_paste.set(PASTE_LABELS.get(c["paste"], PASTE_LABELS["object"]))
        self.v_anchor.set(ANCHOR_LABELS.get(c["anchor"], ANCHOR_LABELS["baseline"]))
        # history entries are {"eq", "shift"}; older versions stored plain strings
        c["history"] = [h if isinstance(h, dict) else {"eq": h, "shift": None} for h in c["history"]]
        self.v_zoom.set(c["zoom"] if c["zoom"] in ZOOMS else "200 %")
        self.v_top.set(bool(c["topmost"]))
        self.v_app.set(c["appearance"] if c["appearance"] in APPEARANCES else "System")
        self.attributes("-topmost", self.v_top.get())
        self.v_tw.set(str(c["tb_width"]))
        self.v_th.set(str(c["tb_height"]))
        self.v_tunit.set(c["tb_unit"] if c["tb_unit"] in UNITS else "mm")
        self.v_align.set(ALIGN_LABELS.get(c["align"], ALIGN_LABELS["justify-left"]))
        self.v_leading.set(str(c["leading"]))
        self.v_parskip.set(str(c["parskip"]))
        self.v_indent.set(str(c["indent"]))
        self.v_hyph.set(bool(c["hyphenate"]))
        self.mode = c["mode"] if c["mode"] in MODES else "math"
        self.v_mode.set(MODES[self.mode])
        self.eq.insert("1.0", c["text"] if self.mode == "text" else c["eq"])
        self._apply_mode()
        self.eq.edit_reset()
        self.eq.edit_modified(False)
        self._refresh_history()
        self._apply_pre_states()

    def _save_cfg(self) -> None:
        self.cfg.update(size=self.v_size.get(), color=self.v_color.get(),
                        font=KEY_FROM_LABEL.get(self.v_font.get(), "cm"), size_pre=self.v_size_pre.get(),
                        color_pre=self.v_color_pre.get(), font_pre=self.v_font_pre.get(),
                        bold=self.v_bold.get(), inline=self.v_inline.get(), border=self.v_border.get(),
                        paste=PASTE_FROM_LABEL.get(self.v_paste.get(), "object"), zoom=self.v_zoom.get(),
                        anchor=ANCHOR_FROM_LABEL.get(self.v_anchor.get(), "baseline"),
                        topmost=self.v_top.get(), appearance=self.v_app.get(), geometry=self.geometry(),
                        mode=self.mode, tb_width=self.v_tw.get(), tb_height=self.v_th.get(),
                        tb_unit=self.v_tunit.get(), align=ALIGN_FROM_LABEL.get(self.v_align.get(), "justify-left"),
                        leading=self.v_leading.get(), parskip=self.v_parskip.get(), indent=self.v_indent.get(),
                        hyphenate=self.v_hyph.get())
        self.cfg["text" if self.mode == "text" else "eq"] = self.equation()
        try:
            with open(CFG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=1, ensure_ascii=False)
        except OSError:
            pass

    def _close(self) -> None:
        self._save_cfg()
        for job in (self._after, self._poll_id):
            if job:
                self.after_cancel(job)
        self.destroy()

    def equation(self) -> str:
        return self.eq.get("1.0", "end").strip()

    def options(self) -> Options:
        """Read the style fields. Raises ValueError (and marks the offending field red)."""
        ok = {"size": True, "color": True, "border": True}
        size, color = FROM_PREAMBLE, FROM_PREAMBLE
        if not self.v_size_pre.get():
            try:
                size = float(self.v_size.get().replace(",", "."))
                if size <= 0:
                    raise ValueError
            except ValueError:
                ok["size"] = False
        if not self.v_color_pre.get():
            try:
                color = normalize_color(self.v_color.get())
            except ValueError:
                ok["color"] = False
        try:
            border = float(self.v_border.get().replace(",", "."))
            if border < 0:
                raise ValueError
        except ValueError:
            ok["border"] = False
        tb = self._text_block_fields(ok)
        normal = ctk.ThemeManager.theme["CTkEntry"]["text_color"]
        for w, k in ((self.e_size, "size"), (self.e_color, "color"), (self.e_border, "border"),
                     (self.e_tw, "width"), (self.e_th, "height"), (self.e_lead, "line spacing"),
                     (self.e_parskip, "paragraph space"), (self.e_indent, "indent")):
            w.configure(text_color=normal if ok.get(k, True) else RED)
        if not all(ok.values()):
            raise ValueError("invalid " + ", ".join(k for k, v in ok.items() if not v))
        self.swatch.configure(fg_color=("#" + color) if color != FROM_PREAMBLE else ("gray75", "gray30"))
        font = FROM_PREAMBLE if self.v_font_pre.get() else KEY_FROM_LABEL.get(self.v_font.get(), "cm")
        return Options(size=size, color=color, font=font, bold=self.v_bold.get(), inline=self.v_inline.get(),
                       border=border, paste=PASTE_FROM_LABEL.get(self.v_paste.get(), "object"),
                       anchor=ANCHOR_FROM_LABEL.get(self.v_anchor.get(), "baseline"), mode=self.mode, **tb)

    # ------------------------------------------------------------------ events
    def _on_modified(self, _e=None) -> None:
        if self.eq.edit_modified():
            self.eq.edit_modified(False)
            self.schedule()

    def _on_style(self) -> None:
        self.update_preamble()
        self.schedule()

    def _on_pre_toggle(self) -> None:
        self._apply_pre_states()
        self._on_style()
        pre = [n for n, v in (("size", self.v_size_pre), ("colour", self.v_color_pre),
                              ("font", self.v_font_pre)) if v.get()]
        self.set_status(("From the preamble file: " + ", ".join(pre) + ". The other fields still override it.")
                        if pre else "All style fields override the preamble file.")

    def _apply_pre_states(self) -> None:
        """Fields taken from the preamble are greyed out."""
        for var, widgets in ((self.v_size_pre, (self.e_size,)), (self.v_color_pre, (self.e_color, self.b_pick)),
                             (self.v_font_pre, (self.o_font,))):
            for w in widgets:
                w.configure(state="disabled" if var.get() else "normal")

    def _on_focus(self, e) -> None:
        if e.widget is self and self._mtime() != self._pre_mtime:   # preamble file was edited
            self._pre_mtime = self._mtime()
            self.update_preamble()
            self.refresh()

    @staticmethod
    def _mtime() -> float:
        try:
            return PREAMBLE_FILE.stat().st_mtime
        except OSError:
            return 0.0

    def schedule(self, delay: int = 500) -> None:
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(delay, self.refresh)

    def _pick_color(self) -> None:
        if self.v_color_pre.get():
            return
        cur = self.v_color.get().lstrip("#")
        rgb, hexv = colorchooser.askcolor(color="#" + (cur if len(cur) == 6 else "000000"),
                                          parent=self, title="Equation colour")
        if hexv:
            self.v_color.set(hexv.lstrip("#").upper())

    def _pick_history(self, choice: str) -> None:
        labels = self._history_labels()
        self.v_hist.set("History")
        if choice in labels:
            i = labels.index(choice)
            entry = self.cfg["history"][i]
            mode = entry.get("mode", "math")
            if mode != self.mode:
                self.v_mode.set(MODES[mode])
                self._on_mode(MODES[mode])
            self.eq.delete("1.0", "end")
            self.eq.insert("1.0", entry["eq"])
            if mode == "math":
                self.show_shift(entry.get("shift"))

    def _history_labels(self) -> list[str]:
        return [f"{i + 1:>2}  " + ("[text] " if h.get("mode") == "text" else "") + h["eq"].replace("\n", " ")[:60]
                for i, h in enumerate(self.cfg["history"])]

    def _refresh_history(self) -> None:
        self.hist.configure(values=self._history_labels() or ["(empty)"])
        self.v_hist.set("History")

    # ------------------------------------------------------------------ preamble
    def update_preamble(self) -> None:
        try:
            opts = self.options()
        except ValueError as e:
            self._set_text(self.pre_txt, f"% {e}")
            return
        try:
            self.preamble_text = build_preamble(opts)
        except ValueError as e:
            self._set_text(self.pre_txt, f"% {e}")
            return
        self._set_text(self.pre_txt, self.preamble_text)
        from_pre = [n for n, v in (("font", self.v_font_pre), ("size", self.v_size_pre),
                                   ("colour", self.v_color_pre)) if v.get()]
        removed = strip_overrides(read_user_preamble(), font=not self.v_font_pre.get(),
                                  size=not self.v_size_pre.get(), color=not self.v_color_pre.get())[1]
        note = f"Preamble file: {PREAMBLE_FILE}\n"
        note += (f"Taken from the preamble file: {', '.join(from_pre)}. " if from_pre else "")
        note += ("Replaced by the window settings: " + ", ".join(removed) + "." if removed
                 else "Nothing in the preamble file is overridden.")
        self.pre_note.configure(text=note)

    def copy_preamble(self) -> None:
        try:
            self.options()
        except ValueError as e:
            return self.set_status(f"Cannot copy preamble: {e}", error=True)
        self.clipboard_clear()
        self.clipboard_append(self.preamble_text)
        self.update()                       # keep the clipboard after the app closes
        self.set_status("Preamble copied to the clipboard (no \\documentclass - fits into any document).")

    def load_preamble(self, path: str | None = None) -> None:
        """Make a file the personal preamble (click: file dialog, or a file dropped on the icon)."""
        if path is None:
            path = filedialog.askopenfilename(
                parent=self, title="Choose a preamble file",
                initialdir=self.cfg.get("last_dir") or str(Path.home()),
                filetypes=[("TeX files", "*.tex *.sty *.cls *.txt"), ("All files", "*.*")])
            if not path:
                return
        try:
            backup = install_preamble(path)
        except (OSError, UnicodeError) as e:
            return self.set_status(f"Could not load {path}: {e}", error=True)
        self.cfg["last_dir"] = str(Path(path).parent)
        self._pre_mtime = self._mtime()
        self.update_preamble()
        self.refresh()
        msg = f"Loaded {Path(path).name} as preamble"
        self.set_status(msg + (f" - previous one saved as {backup.name}." if backup else "."))
        self._log(f"preamble <- {path}" + (f"\nbackup: {backup}" if backup else ""))
        self.icon_hint.configure(text="loaded!", text_color=TEAL)
        self.after(1800, lambda: self.icon_hint.configure(text=self.icon_hint_text, text_color=MUTED))

    def _on_drop(self, e):
        self._hover_icon(False)
        files = [f for f in self.tk.splitlist(e.data) if os.path.isfile(f)]
        if files:
            self.load_preamble(files[0])
        return e.action

    def edit_preamble_file(self) -> None:
        if not PREAMBLE_FILE.exists():
            PREAMBLE_FILE.write_text(PREAMBLE_STUB, encoding="utf-8")
        try:
            open_path(PREAMBLE_FILE)
        except OSError:
            subprocess.Popen(["notepad", str(PREAMBLE_FILE)])

    # ------------------------------------------------------------------ preview / copy (worker threads)
    def refresh(self) -> None:
        self._after = None
        eq = self.equation()
        try:
            opts = self.options()
        except ValueError as e:
            return self.set_status(str(e), error=True)
        if not eq:
            self._img, self._error = None, ""
            self._draw()
            return self.set_status("Type an equation.")
        self.gen += 1
        zoom = int((self.v_zoom.get() or "200").split()[0])
        gen, dpi = self.gen, int(96 * zoom / 100)
        self._busy += 1
        self.set_status("Compiling...")

        def work() -> None:
            try:
                png, dims = render_preview(eq, opts, dpi=dpi)
                self.q.put(("preview", gen, (png, baseline_shift(dims, opts), text_overflow(dims, opts))))
            except Exception as e:  # noqa: BLE001 - show anything LaTeX/IO throws
                self.q.put(("error", gen, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def copy(self) -> None:
        eq = self.equation()
        if not eq:
            return self.set_status("Nothing to copy.", error=True)
        try:
            opts = self.options()
        except ValueError as e:
            return self.set_status(str(e), error=True)
        self.btn_copy.configure(state="disabled")
        self.set_status("Building vector and copying...")

        def work() -> None:
            try:
                res = build_result(eq, opts)
                copied = to_clipboard(res.path, opts.paste)
                self.q.put(("copied", eq, (res.path, opts.paste, copied, res.shift, opts.mode, res.overflow)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("copy_error", 0, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        if ctk.get_appearance_mode() != self._mode:          # e.g. Windows switched to dark
            self._mode = ctk.get_appearance_mode()
            self._recolor()
        try:
            while True:
                kind, key, payload = self.q.get_nowait()
                if kind in ("preview", "error"):
                    self._busy = max(0, self._busy - 1)
                    if key == self.gen:           # results of outdated requests are dropped
                        if kind == "preview":
                            png, shift, overflow = payload
                            self._img = tk.PhotoImage(data=base64.b64encode(png))
                            self._error = ""
                            if self.mode == "text":
                                self.show_frame(overflow)
                            else:
                                self.show_shift(shift)
                        else:
                            self._error = payload
                        self._log(("OK" if kind == "preview" else payload))
                        self._draw()
                    if self._busy:                # status always follows the number of pending jobs
                        self.set_status("Compiling...")
                    elif self._error:
                        self.set_status("LaTeX error - see the preview or log.", error=True)
                    else:
                        self.set_status("Preview up to date.")
                elif kind == "copied":
                    path, mode, copied, shift, kind_, overflow = payload
                    self.btn_copy.configure(state="normal")
                    self._add_history(key, shift, kind_)
                    if kind_ == "text":
                        self.show_frame(overflow)
                    else:
                        self.show_shift(shift)
                    self.set_status(f"Copied as {mode} - press Ctrl+V in Affinity." if copied
                                    else f"Saved {path} (no clipboard support here).")
                    self._log(f"wrote {path}")
                elif kind == "copy_error":
                    self.btn_copy.configure(state="normal")
                    self.set_status("Copy failed - see log.", error=True)
                    self._error = payload
                    self._log(payload)
                    self._draw()
        except queue.Empty:
            pass
        self._poll_id = self.after(60, self._poll)

    # ------------------------------------------------------------------ drawing & small helpers
    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        try:   # light equation colour -> dark paper, otherwise white paper (independent of the theme)
            dark = not self.v_color_pre.get() and luminance(normalize_color(self.v_color.get())) > 0.7
        except ValueError:
            dark = False
        c.configure(bg="#26292c" if dark else "#ffffff")
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        if self._error:
            c.create_text(12, 12, anchor="nw", text=self._error, fill="#ff6b5b" if dark else "#c0392b",
                          font=("Consolas", 11), width=w - 24)
            c.configure(scrollregion=(0, 0, w, h))
        elif self._img:
            iw, ih = self._img.width(), self._img.height()
            x, y = max(self.PAD, (w - iw) // 2), max(self.PAD, (h - ih) // 2)
            c.create_image(x, y, image=self._img, anchor="nw")
            c.configure(scrollregion=(0, 0, max(w, iw + 2 * self.PAD), max(h, ih + 2 * self.PAD)))
        else:
            c.create_text(w // 2, h // 2, text="Preview appears here", fill="#999", font=("Segoe UI", 12))

    def _add_history(self, eq: str, shift, mode: str = "math") -> None:
        h = [x for x in self.cfg["history"] if x["eq"] != eq]
        self.cfg["history"] = ([{"eq": eq, "shift": shift, "mode": mode}] + h)[:30]
        self._refresh_history()

    # ------------------------------------------------------------------ text blocks
    def _build_text_block(self, left, bold) -> None:
        """Frame size, alignment and spacing of a text block (shown in "Text block" mode)."""
        tf = self.tf = ctk.CTkFrame(left)
        tf.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        tf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tf, text="Text block", font=bold).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        ctk.CTkLabel(tf, text="pasted at exactly this size",
                     text_color=MUTED).grid(row=0, column=1, sticky="e", padx=12, pady=(8, 2))
        self.v_tw, self.v_th, self.v_tunit = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.v_align, self.v_leading = tk.StringVar(), tk.StringVar()
        self.v_parskip, self.v_indent, self.v_hyph = tk.StringVar(), tk.StringVar(), tk.BooleanVar()

        def label(r, text):
            ctk.CTkLabel(tf, text=text).grid(row=r, column=0, sticky="w", padx=12, pady=4)

        label(1, "Frame")
        r = ctk.CTkFrame(tf, fg_color="transparent")
        r.grid(row=1, column=1, sticky="w")
        self.e_tw = ctk.CTkEntry(r, width=64, textvariable=self.v_tw)
        self.e_tw.pack(side="left")
        ctk.CTkLabel(r, text="\u00d7").pack(side="left", padx=6)
        self.e_th = ctk.CTkEntry(r, width=64, textvariable=self.v_th)
        self.e_th.pack(side="left")
        ctk.CTkOptionMenu(r, variable=self.v_tunit, values=UNITS, width=70,
                          command=lambda _: self._on_style()).pack(side="left", padx=8)
        ctk.CTkLabel(r, text="height empty = auto", text_color=MUTED).pack(side="left")
        label(2, "Alignment")
        ctk.CTkOptionMenu(tf, variable=self.v_align, values=list(ALIGN_LABELS.values()), dynamic_resizing=False,
                          command=lambda _: self._on_style()).grid(row=2, column=1, sticky="ew", padx=(0, 12))
        label(3, "Spacing")
        r = ctk.CTkFrame(tf, fg_color="transparent")
        r.grid(row=3, column=1, sticky="w")
        ctk.CTkLabel(r, text="line").pack(side="left", padx=(0, 6))
        self.e_lead = ctk.CTkEntry(r, width=50, textvariable=self.v_leading)
        self.e_lead.pack(side="left")
        ctk.CTkLabel(r, text="\u00d7 size").pack(side="left", padx=(6, 16))
        ctk.CTkLabel(r, text="paragraph gap").pack(side="left", padx=(0, 6))
        self.e_parskip = ctk.CTkEntry(r, width=50, textvariable=self.v_parskip)
        self.e_parskip.pack(side="left")
        ctk.CTkLabel(r, text="pt").pack(side="left", padx=(6, 0))
        label(4, "Options")
        r = ctk.CTkFrame(tf, fg_color="transparent")
        r.grid(row=4, column=1, sticky="w", pady=(0, 10))
        ctk.CTkCheckBox(r, text="Hyphenation", variable=self.v_hyph, width=120,
                        command=self._on_style).pack(side="left")
        ctk.CTkLabel(r, text="First-line indent").pack(side="left", padx=(8, 6))
        self.e_indent = ctk.CTkEntry(r, width=50, textvariable=self.v_indent)
        self.e_indent.pack(side="left")
        ctk.CTkLabel(r, text="pt").pack(side="left", padx=6)
        for v in (self.v_tw, self.v_th, self.v_leading, self.v_parskip, self.v_indent):
            v.trace_add("write", lambda *_: self._on_style())

    def _text_block_fields(self, ok: dict) -> dict:
        """Text block settings as Options fields; marks invalid entries in `ok`."""
        if self.mode != "text":
            return {}
        unit = self.v_tunit.get() or "mm"
        out = {"align": ALIGN_FROM_LABEL.get(self.v_align.get(), "justify-left"), "hyphenate": self.v_hyph.get()}
        for key, var, empty_ok in (("width", self.v_tw, False), ("height", self.v_th, True)):
            v = var.get().strip().replace(",", ".")
            if not v and empty_ok:
                out[key] = ""
                continue
            try:
                tex_dim(v + unit)
                out[key] = v + unit
            except ValueError:
                ok[key] = False
        for key, var, name, positive in (("leading", self.v_leading, "line spacing", True),
                                         ("parskip", self.v_parskip, "paragraph space", False),
                                         ("indent", self.v_indent, "indent", False)):
            try:
                v = float(var.get().replace(",", ".") or 0)
                if v < 0 or (positive and v == 0):
                    raise ValueError
                out[key] = v
            except ValueError:
                ok[name] = False
        return out

    def _on_mode(self, label: str) -> None:
        mode = "text" if label == MODES["text"] else "math"
        if mode == self.mode:
            return
        self.cfg["text" if self.mode == "text" else "eq"] = self.equation()   # keep both texts
        self.mode = mode
        self.eq.delete("1.0", "end")
        self.eq.insert("1.0", self.cfg["text"] if mode == "text" else self.cfg["eq"])
        self._apply_mode()
        self._on_style()

    def _apply_mode(self) -> None:
        text = self.mode == "text"
        if text:
            self.tf.grid()
        else:
            self.tf.grid_remove()
        self.eq_hint.configure(text="math in $...$, empty line = paragraph" if text
                               else "math mode, no $...$   \u00b7   Ctrl+Enter copies")
        try:
            self.eq.configure(wrap="word" if text else "none")
        except (ValueError, tk.TclError):
            pass
        for w in (self.cb_inline, self.o_anchor):
            w.configure(state="disabled" if text else "normal")
        self.b_shift.configure(state="disabled")
        if text:
            self.b_shift.pack_forget()
        else:
            self.b_shift.pack(side="left", padx=12)
        self.shift_hint.configure(
            text="The pasted object has exactly the frame size (at 100 %). Text is curves, edit it here."
            if text else f"Paste the equation first, then copy this value into Character panel {ARROW} "
                         "Baseline shift. Valid when the equation is pasted at 100 %.")

    def show_frame(self, overflow) -> None:
        """Text block info under the preview: does the text fit the fixed frame height?"""
        normal = ctk.ThemeManager.theme["CTkLabel"]["text_color"]
        if overflow is None:
            self.shift_label.configure(text="Text block: fits the frame", text_color=normal)
        else:
            self.shift_label.configure(text=f"Text is {overflow:.1f} pt taller than the frame", text_color=RED)

    def show_shift(self, shift) -> None:
        """Baseline shift line under the preview (a dash for anchor "ink" or when unknown)."""
        self.shift = shift
        self.shift_label.configure(text_color=ctk.ThemeManager.theme["CTkLabel"]["text_color"])
        if shift is None:
            self.shift_label.configure(text=f"Baseline shift: {DASH}")
            self.b_shift.configure(state="disabled")
        else:
            self.shift_label.configure(text=f"Baseline shift: {shift:.2f} pt".replace("-", MINUS))
            self.b_shift.configure(state="normal")

    def copy_shift(self) -> None:
        """Only the number with an ASCII minus, for Affinity's Baseline shift field."""
        if self.shift is None:
            return
        self.clipboard_clear()
        self.clipboard_append(f"{self.shift:.2f}")
        self.update()
        self.set_status(f"Copied {self.shift:.2f} - paste it into Character panel {ARROW} Baseline shift.")

    @staticmethod
    def _set_text(widget, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg.strip() + "\n\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, msg: str, error: bool = False) -> None:
        self.status.configure(text=msg, text_color=RED if error else ctk.ThemeManager.theme["CTkLabel"]["text_color"])


def main() -> None:
    if sys.platform == "win32":
        try:   # process-wide ID (enough for python.org Python; the window ID covers Store Python)
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass
    _theme()
    App().mainloop()


if __name__ == "__main__":
    main()
