"""TeXclip GUI: type LaTeX math, see it rendered, copy it as a vector object for Affinity.

tkinter only (stdlib). Optional: ``pip install tkinterdnd2`` enables dragging the preamble icon
into other applications as text.
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
from tkinter import colorchooser, messagebox, ttk

from . import __version__
from .shortcut import APP_ID, ICON_ICO, ICON_PNG
from .core import (FONTS, OUTDIR, PREAMBLE_FILE, Options, build, build_preamble, missing_tools,
                   normalize_color, read_user_preamble, render_png, strip_overrides, to_clipboard)

try:  # optional drag & drop
    from tkinterdnd2 import DND_TEXT, TkinterDnD
    _Base = TkinterDnD.Tk
except Exception:  # pragma: no cover
    DND_TEXT, _Base = None, tk.Tk

CFG_FILE = os.path.join(os.path.expanduser("~"), ".texclip_gui.json")
FONT_LABELS = {k: f"{k} - {d}" for k, (_, d) in FONTS.items()}
KEY_FROM_LABEL = {v: k for k, v in FONT_LABELS.items()}
PASTE_LABELS = {"object": "One vector object (scales as a whole)",
                "curves": "Curves (every glyph editable)",
                "both": "Both (pastes two objects)"}
PASTE_FROM_LABEL = {v: k for k, v in PASTE_LABELS.items()}
ZOOMS = ["100", "150", "200", "300", "400"]
MONO = ("Consolas", 12) if sys.platform == "win32" else ("Menlo", 12)
PREAMBLE_STUB = ("% TeXclip personal preamble - inserted after the base packages.\n"
                 "% Font, size and colour are set in the TeXclip window and override anything set here.\n"
                 "% Example:\n% \\newcommand{\\Hamil}{\\hat{H}}\n")
DEFAULTS = dict(size="10", color="000000", font="cm", bold=False, inline=False, border="0.5",
                paste="object", zoom="200", topmost=False, history=[], eq=r"i\hbar\,\partial_t\ket{\psi}=\hat H\ket{\psi}",
                geometry="1120x700")


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


class App(_Base):
    PAD = 16

    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_cfg()
        self.title(f"TeXclip {__version__}")
        self._set_icon()
        self.geometry(self.cfg["geometry"])
        self.minsize(820, 520)
        self.q: queue.Queue = queue.Queue()
        self.gen = 0                     # id of the newest preview request; older results are dropped
        self._after = None
        self._img = None
        self._error = ""
        self._busy = 0
        self._pre_mtime = self._mtime()
        self.preamble_text = ""

        self._build_ui()
        self._load_into_ui()
        self._poll_id = self.after(60, self._poll)
        self.bind("<FocusIn>", self._on_focus)
        self.protocol("WM_DELETE_WINDOW", self._close)
        if missing_tools(("latex", "dvisvgm")):
            messagebox.showwarning("TeX not found", "latex / dvisvgm were not found on PATH.\n"
                                   "Install MiKTeX or TeX Live and restart TeXclip.")
        self.update_preamble()
        self.refresh()

    def _set_icon(self) -> None:
        """TeXclip icon in the title bar, the taskbar and Alt+Tab (replaces the tkinter feather)."""
        try:
            if sys.platform == "win32":
                self.iconbitmap(default=str(ICON_ICO))
            else:
                self._icon_img = tk.PhotoImage(file=str(ICON_PNG))   # keep a reference
                self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass                                                    # missing asset: keep the default

    # ------------------------------------------------------------------ UI construction
    def _build_ui(self) -> None:
        ttk.Style(self).theme_use("vista" if sys.platform == "win32" else "clam")
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        self.icon = self._make_preamble_icon(bar)
        self.icon.pack(side="left")
        txt = ttk.Frame(bar)
        txt.pack(side="left", padx=10)
        ttk.Label(txt, text="Preamble", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.icon_hint = ttk.Label(txt, text="click the icon to copy it", foreground="#666")
        self.icon_hint.pack(anchor="w")
        self.v_top = tk.BooleanVar()
        ttk.Checkbutton(bar, text="Always on top", variable=self.v_top,
                        command=lambda: self.attributes("-topmost", self.v_top.get())).pack(side="right")
        self.v_hist = tk.StringVar()
        self.hist = ttk.Combobox(bar, textvariable=self.v_hist, state="readonly", width=34)
        self.hist.pack(side="right", padx=14)
        self.hist.bind("<<ComboboxSelected>>", self._pick_history)
        ttk.Label(bar, text="History").pack(side="right")

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10)
        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        right = ttk.Frame(paned, padding=(8, 0, 0, 0))
        paned.add(left, weight=1)
        paned.add(right, weight=2)

        # equation editor
        ef = ttk.LabelFrame(left, text="Equation  (math mode, no $...$)", padding=6)
        ef.pack(fill="both", expand=True)
        self.eq = tk.Text(ef, height=9, width=40, font=MONO, undo=True, wrap="none", relief="flat",
                          highlightthickness=1, highlightbackground="#bbb", highlightcolor="#0f4c5c")
        self.eq.pack(fill="both", expand=True)
        self.eq.bind("<<Modified>>", self._on_modified)
        self.eq.bind("<Control-Return>", lambda e: (self.copy(), "break")[1])
        self.bind("<F5>", lambda e: self.refresh())

        # style controls
        sf = ttk.LabelFrame(left, text="Style  (overrides the preamble file)", padding=8)
        sf.pack(fill="x", pady=(8, 0))
        sf.columnconfigure(1, weight=1)
        self.v_size, self.v_color, self.v_font = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.v_border, self.v_paste = tk.StringVar(), tk.StringVar()
        self.v_bold, self.v_inline = tk.BooleanVar(), tk.BooleanVar()

        ttk.Label(sf, text="Font size").grid(row=0, column=0, sticky="w", pady=3)
        r = ttk.Frame(sf)
        r.grid(row=0, column=1, sticky="w")
        self.e_size = ttk.Spinbox(r, from_=1, to=500, increment=1, width=7, textvariable=self.v_size)
        self.e_size.pack(side="left")
        ttk.Label(r, text="pt").pack(side="left", padx=4)

        ttk.Label(sf, text="Colour").grid(row=1, column=0, sticky="w", pady=3)
        r = ttk.Frame(sf)
        r.grid(row=1, column=1, sticky="w")
        ttk.Label(r, text="#").pack(side="left")
        self.e_color = ttk.Entry(r, width=9, textvariable=self.v_color)
        self.e_color.pack(side="left")
        self.swatch = tk.Label(r, width=3, relief="solid", bd=1)
        self.swatch.pack(side="left", padx=6)
        self.swatch.bind("<Button-1>", lambda e: self._pick_color())
        ttk.Button(r, text="Pick...", width=7, command=self._pick_color).pack(side="left")

        ttk.Label(sf, text="Font").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(sf, textvariable=self.v_font, values=list(FONT_LABELS.values()),
                     state="readonly").grid(row=2, column=1, sticky="ew")

        ttk.Label(sf, text="Options").grid(row=3, column=0, sticky="w", pady=3)
        r = ttk.Frame(sf)
        r.grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(r, text="Bold", variable=self.v_bold).pack(side="left")
        ttk.Checkbutton(r, text="Inline style", variable=self.v_inline).pack(side="left", padx=10)
        ttk.Label(r, text="Border").pack(side="left", padx=(6, 3))
        self.e_border = ttk.Spinbox(r, from_=0, to=50, increment=0.5, width=5, textvariable=self.v_border)
        self.e_border.pack(side="left")
        ttk.Label(r, text="pt").pack(side="left", padx=3)

        ttk.Label(sf, text="Paste as").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Combobox(sf, textvariable=self.v_paste, values=list(PASTE_LABELS.values()),
                     state="readonly").grid(row=4, column=1, sticky="ew")

        for v in (self.v_size, self.v_color, self.v_font, self.v_border, self.v_bold, self.v_inline):
            v.trace_add("write", lambda *_: self._on_style())

        # actions
        ab = ttk.Frame(left)
        ab.pack(fill="x", pady=8)
        self.btn_copy = ttk.Button(ab, text="Copy to clipboard   Ctrl+Enter", command=self.copy)
        self.btn_copy.pack(side="left", fill="x", expand=True)
        ttk.Button(ab, text="Output folder", command=lambda: (OUTDIR.mkdir(parents=True, exist_ok=True),
                                                              open_path(OUTDIR))).pack(side="left", padx=(6, 0))
        ttk.Button(ab, text="Edit preamble file", command=self.edit_preamble_file).pack(side="left", padx=(6, 0))

        # right: tabs
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        pv = ttk.Frame(nb)
        nb.add(pv, text="Preview")
        tb = ttk.Frame(pv, padding=(6, 4))
        tb.pack(fill="x")
        ttk.Label(tb, text="Zoom").pack(side="left")
        self.v_zoom = tk.StringVar()
        z = ttk.Combobox(tb, textvariable=self.v_zoom, values=ZOOMS, width=5, state="readonly")
        z.pack(side="left", padx=4)
        ttk.Label(tb, text="%   (preview only - the exported vector is size-independent)",
                  foreground="#666").pack(side="left")
        z.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        wrap = ttk.Frame(pv)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg="white")
        xs = ttk.Scrollbar(wrap, orient="horizontal", command=self.canvas.xview)
        ys = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xs.set, yscrollcommand=ys.set)
        xs.pack(side="bottom", fill="x")
        ys.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw())

        pt = ttk.Frame(nb)
        nb.add(pt, text="Preamble")
        self.pre_txt = tk.Text(pt, font=MONO, wrap="none", height=10, relief="flat", state="disabled")
        self.pre_txt.pack(fill="both", expand=True, padx=4, pady=4)
        self.pre_note = ttk.Label(pt, foreground="#666", wraplength=520, justify="left")
        self.pre_note.pack(fill="x", padx=6, pady=(0, 6))

        lg = ttk.Frame(nb)
        nb.add(lg, text="Log")
        self.log = tk.Text(lg, font=MONO, wrap="word", height=10, relief="flat", state="disabled")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)

        self.status = ttk.Label(self, text="Ready", anchor="w", padding=(10, 4), relief="sunken")
        self.status.pack(fill="x", side="bottom")

    def _make_preamble_icon(self, parent) -> tk.Canvas:
        """Document icon with a folded corner; click copies the personalised preamble."""
        c = tk.Canvas(parent, width=46, height=56, highlightthickness=0, cursor="hand2")
        self._draw_icon(c, hover=False)
        c.bind("<Enter>", lambda e: self._draw_icon(c, True))
        c.bind("<Leave>", lambda e: self._draw_icon(c, False))
        c.bind("<ButtonRelease-1>", lambda e: self.copy_preamble())
        if DND_TEXT:  # drag the preamble as text into any editor
            try:
                c.drag_source_register(1, DND_TEXT)
                c.dnd_bind("<<DragInitCmd>>", lambda e: ("copy", DND_TEXT, self.preamble_text))
                self.icon_hint_text = "click to copy, or drag it into your editor"
            except Exception:
                self.icon_hint_text = "click the icon to copy it"
        return c

    @staticmethod
    def _draw_icon(c: tk.Canvas, hover: bool) -> None:
        c.delete("all")
        fill, edge = ("#fb8b24", "#9a031e") if hover else ("#ffffff", "#0f4c5c")
        c.create_polygon(6, 4, 32, 4, 42, 14, 42, 52, 6, 52, fill=fill, outline=edge, width=2)
        c.create_polygon(32, 4, 32, 14, 42, 14, fill="#d9d9d9", outline=edge, width=2)
        for i, w in enumerate((26, 20, 26, 14)):
            c.create_line(12, 24 + 7 * i, 12 + w, 24 + 7 * i, fill=edge, width=2)
        c.create_text(15, 16, text="\\", font=("Consolas", 12, "bold"), fill=edge)

    # ------------------------------------------------------------------ state <-> UI
    def _load_into_ui(self) -> None:
        c = self.cfg
        self.v_size.set(str(c["size"]))
        self.v_color.set(c["color"])
        self.v_font.set(FONT_LABELS.get(c["font"], FONT_LABELS["cm"]))
        self.v_bold.set(bool(c["bold"]))
        self.v_inline.set(bool(c["inline"]))
        self.v_border.set(str(c["border"]))
        self.v_paste.set(PASTE_LABELS.get(c["paste"], PASTE_LABELS["object"]))
        self.v_zoom.set(str(c["zoom"]))
        self.v_top.set(bool(c["topmost"]))
        self.attributes("-topmost", self.v_top.get())
        self.eq.insert("1.0", c["eq"])
        self.eq.edit_reset()
        self.eq.edit_modified(False)
        self._refresh_history()
        self.icon_hint.configure(text=getattr(self, "icon_hint_text", "click the icon to copy it"))

    def _save_cfg(self) -> None:
        self.cfg.update(size=self.v_size.get(), color=self.v_color.get(),
                        font=KEY_FROM_LABEL.get(self.v_font.get(), "cm"), bold=self.v_bold.get(),
                        inline=self.v_inline.get(), border=self.v_border.get(),
                        paste=PASTE_FROM_LABEL.get(self.v_paste.get(), "object"), zoom=self.v_zoom.get(),
                        topmost=self.v_top.get(), eq=self.equation(), geometry=self.geometry())
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
        try:
            size = float(self.v_size.get().replace(",", "."))
            if size <= 0:
                raise ValueError
        except ValueError:
            ok["size"] = False
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
        for w, k in ((self.e_size, "size"), (self.e_color, "color"), (self.e_border, "border")):
            w.configure(foreground="black" if ok[k] else "#c0392b")
        if not all(ok.values()):
            raise ValueError("invalid " + ", ".join(k for k, v in ok.items() if not v))
        self.swatch.configure(bg="#" + color)
        return Options(size=size, color=color, font=KEY_FROM_LABEL.get(self.v_font.get(), "cm"),
                       bold=self.v_bold.get(), inline=self.v_inline.get(), border=border,
                       paste=PASTE_FROM_LABEL.get(self.v_paste.get(), "object"))

    # ------------------------------------------------------------------ events
    def _on_modified(self, _e=None) -> None:
        if self.eq.edit_modified():
            self.eq.edit_modified(False)
            self.schedule()

    def _on_style(self) -> None:
        self.update_preamble()
        self.schedule()

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
        init = "#" + (self.v_color.get().lstrip("#") if len(self.v_color.get().lstrip("#")) == 6 else "000000")
        rgb, hexv = colorchooser.askcolor(color=init, parent=self, title="Equation colour")
        if hexv:
            self.v_color.set(hexv.lstrip("#").upper())

    def _pick_history(self, _e=None) -> None:
        i = self.hist.current()
        if 0 <= i < len(self.cfg["history"]):
            self.eq.delete("1.0", "end")
            self.eq.insert("1.0", self.cfg["history"][i])

    def _refresh_history(self) -> None:
        self.hist["values"] = [h.replace("\n", " ")[:70] for h in self.cfg["history"]]

    # ------------------------------------------------------------------ preamble
    def update_preamble(self) -> None:
        try:
            opts = self.options()
        except ValueError as e:
            self._set_text(self.pre_txt, f"% {e}")
            return
        self.preamble_text = build_preamble(opts)
        self._set_text(self.pre_txt, self.preamble_text)
        removed = strip_overrides(read_user_preamble())[1]
        self.pre_note.configure(text=(
            "Copied text has no \\documentclass, so it fits into any document.\n"
            "Font, size and colour from the fields above replace those in your preamble file"
            + (f" (replaced: {', '.join(removed)})." if removed else ".")))

    def copy_preamble(self) -> None:
        try:
            self.options()
        except ValueError as e:
            return self.set_status(f"Cannot copy preamble: {e}", error=True)
        self.clipboard_clear()
        self.clipboard_append(self.preamble_text)
        self.update()                       # keep the clipboard after the app closes
        self.set_status("Preamble copied to the clipboard.")
        self.icon_hint.configure(text="copied!", foreground="#0f4c5c")
        self.after(1500, lambda: self.icon_hint.configure(
            text=getattr(self, "icon_hint_text", "click the icon to copy it"), foreground="#666"))

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
        gen, dpi = self.gen, int(96 * int(self.v_zoom.get() or 200) / 100)
        self._busy += 1
        self.set_status("Compiling...")

        def work() -> None:
            try:
                self.q.put(("preview", gen, render_png(eq, opts, dpi=dpi)))
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
        self.btn_copy.state(["disabled"])
        self.set_status("Building vector and copying...")

        def work() -> None:
            try:
                path = build(eq, opts)
                copied = to_clipboard(path, opts.paste)
                self.q.put(("copied", eq, (path, opts.paste, copied)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("copy_error", 0, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, key, payload = self.q.get_nowait()
                if kind in ("preview", "error"):
                    self._busy = max(0, self._busy - 1)
                    if key == self.gen:           # results of outdated requests are dropped
                        if kind == "preview":
                            self._img = tk.PhotoImage(data=base64.b64encode(payload))
                            self._error = ""
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
                    path, mode, copied = payload
                    self.btn_copy.state(["!disabled"])
                    self._add_history(key)
                    self.set_status(f"Copied as {mode} - press Ctrl+V in Affinity." if copied
                                    else f"Saved {path} (no clipboard support here).")
                    self._log(f"wrote {path}")
                elif kind == "copy_error":
                    self.btn_copy.state(["!disabled"])
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
        try:
            dark = luminance(normalize_color(self.v_color.get())) > 0.7
        except ValueError:
            dark = False
        c.configure(bg="#2b2f33" if dark else "#ffffff")
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        if self._error:
            c.create_text(12, 12, anchor="nw", text=self._error, fill="#e74c3c" if dark else "#c0392b",
                          font=MONO, width=w - 24)
            c.configure(scrollregion=(0, 0, w, h))
        elif self._img:
            iw, ih = self._img.width(), self._img.height()
            x, y = max(self.PAD, (w - iw) // 2), max(self.PAD, (h - ih) // 2)
            c.create_image(x, y, image=self._img, anchor="nw")
            c.configure(scrollregion=(0, 0, max(w, iw + 2 * self.PAD), max(h, ih + 2 * self.PAD)))
        else:
            c.create_text(w // 2, h // 2, text="Preview appears here", fill="#999", font=("Segoe UI", 12))

    def _add_history(self, eq: str) -> None:
        h = [x for x in self.cfg["history"] if x != eq]
        self.cfg["history"] = ([eq] + h)[:30]
        self._refresh_history()

    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
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
        self.status.configure(text=msg, foreground="#c0392b" if error else "black")


def main() -> None:
    if sys.platform == "win32":                      # crisp text on high-DPI screens
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
        try:                                         # own taskbar button + icon instead of python.exe's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
