import bisect
import os
import re
import tkinter as tk
from datetime import timedelta
from tkinter import filedialog

import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

_STEP = 0.1
_ZOOM = 0.9
_CTRL_MASK = 0x0004
_SHIFT_MASK = 0x0001
_TIME_FORMAT = "%m-%d %H:%M"


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


def _zoom_or_pan(lo, hi, direction, zoom):
    span = hi - lo
    if span <= 0:
        return lo, hi
    if zoom:
        factor = _ZOOM if direction > 0 else 1 / _ZOOM
        mid = (lo + hi) / 2.0
        return mid - span * factor / 2.0, mid + span * factor / 2.0
    step = span * _STEP * direction
    return lo + step, hi + step


def _draw(ax, times, values, title, ylabel, ylim, window_minutes, now, font):
    if times:
        ax.plot(times, values, marker="o", markersize=3, linewidth=1)
        ax.xaxis.set_major_formatter(mdates.DateFormatter(_TIME_FORMAT))
    else:
        ax.text(0.5, 0.5, "No data in this window", ha="center", va="center",
                transform=ax.transAxes, fontsize=font, color="gray")
    if ylim is not None:
        lo, hi = ylim
        ax.set_ylim(lo, hi)
        ax.set_yticks([lo + (hi - lo) * i / 5 for i in range(6)])
    if window_minutes is not None and now is not None:
        lo = mdates.date2num(now - timedelta(minutes=window_minutes))
        hi = mdates.date2num(now)
        ax.set_xlim(lo, hi)
        ax.set_xticks([lo + (hi - lo) * i / 10 for i in range(11)])
        for label in ax.get_xticklabels():
            label.set_rotation(30)
            label.set_ha("right")
    ax.grid(True, which="major", linestyle="-", linewidth=0.5, alpha=0.4)
    ax.set_title(title, fontsize=font + 1)
    ax.set_ylabel(ylabel, fontsize=font)


class ChartWidget:
    def __init__(self, parent, chart_id: str, height=2.6, on_save=None, on_save_as=None):
        self.chart_id = chart_id
        self.on_save = on_save
        self.on_save_as = on_save_as or self._save_as
        self.zoom_override = None
        self.xzoom_override = None
        self._last_args = None

        self.wrapper = tk.Frame(parent, height=int(height * 100))
        self.wrapper.pack(fill="both", expand=True, pady=3)
        self.wrapper.pack_propagate(False)

        self.fig = Figure(figsize=(8, height), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.wrapper)
        widget = self.canvas.get_tk_widget()
        widget.pack(fill="both", expand=True)

        self._attach_scroll(widget)
        self._attach_hover(widget)

        self.tooltip = tk.Label(self.wrapper, bg="#ffffe0", fg="black", bd=1, relief="solid",
                                font=("TkDefaultFont", 8), justify="left", padx=3, pady=1)

        bar = tk.Frame(self.wrapper)
        bar.place(relx=1.0, x=-3, y=3, anchor="ne")
        if on_save is not None:
            tk.Button(bar, text="\U0001F4BE", width=2,
                      command=lambda: on_save(self.fig, self.chart_id)).pack(side="left", padx=1)
        tk.Button(bar, text="⟲", width=2, command=self._reset_zoom).pack(side="left", padx=1)
        tk.Button(bar, text="⛶", width=2, command=self._open_fullscreen).pack(side="left", padx=1)

    def _attach_scroll(self, widget):
        def wheel(direction, ctrl, shift):
            if shift:
                lo, hi = _zoom_or_pan(*(self.xzoom_override or self.ax.get_xlim()), direction, ctrl)
                self.xzoom_override = (lo, hi)
                self.ax.set_xlim(lo, hi)
            else:
                lo, hi = _zoom_or_pan(*(self.zoom_override or self.ax.get_ylim()), direction, ctrl)
                self.zoom_override = (lo, hi)
                self.ax.set_ylim(lo, hi)
            self.canvas.draw_idle()

        def on_wheel(event, direction=None):
            if direction is None:
                direction = 1 if getattr(event, "delta", 0) > 0 else -1
            wheel(direction, bool(event.state & _CTRL_MASK), bool(event.state & _SHIFT_MASK))
            return "break"

        def on_reset(_event):
            self._reset_zoom()

        widget.bind("<MouseWheel>", on_wheel)
        widget.bind("<Button-4>", lambda e: on_wheel(e, 1))
        widget.bind("<Button-5>", lambda e: on_wheel(e, -1))
        widget.bind("<Button-3>", on_reset)

    def _attach_hover(self, widget):
        def on_motion(event):
            if not self._last_args or not self._last_args[0]:
                self.tooltip.place_forget()
                return
            height = self.fig.bbox.height
            try:
                ax_x, ax_y = self.ax.transAxes.inverted().transform((event.x, height - event.y))
                xdata, _ = self.ax.transData.inverted().transform((event.x, height - event.y))
            except Exception:
                self.tooltip.place_forget()
                return
            if not (0.0 <= ax_x <= 1.0 and 0.0 <= ax_y <= 1.0):
                self.tooltip.place_forget()
                return

            times, values = self._last_args[0], self._last_args[1]
            xnums = mdates.date2num(times)
            idx = bisect.bisect_left(xnums, xdata)
            if idx >= len(xnums):
                idx = len(xnums) - 1
            elif idx > 0 and (xdata - xnums[idx - 1]) <= (xnums[idx] - xdata):
                idx -= 1

            value = values[idx]
            if value != value:
                self.tooltip.place_forget()
                return
            self.tooltip.config(text=f"{times[idx].strftime('%m-%d %H:%M:%S')}\n{value:.3g}")
            self.tooltip.place(x=min(event.x + 12, max(widget.winfo_width() - 90, 0)),
                               y=min(event.y + 12, max(widget.winfo_height() - 34, 0)))

        widget.bind("<Motion>", on_motion, add="+")
        widget.bind("<Leave>", lambda _e: self.tooltip.place_forget(), add="+")

    def plot(self, times, values, title, ylabel, ylim, window_minutes, now):
        self._last_args = (times, values, title, ylabel, ylim, window_minutes, now)
        self.ax.clear()
        _draw(self.ax, times, values, title, ylabel, self.zoom_override or ylim,
              window_minutes, now, font=8)
        if self.xzoom_override:
            self.ax.set_xlim(*self.xzoom_override)
        self.ax.tick_params(labelsize=7)
        try:
            self.fig.tight_layout()
        except Exception:
            pass
        self.canvas.draw()

    def _reset_zoom(self):
        self.zoom_override = None
        self.xzoom_override = None
        if self._last_args:
            self.plot(*self._last_args)
        else:
            self.canvas.draw_idle()

    def _open_fullscreen(self):
        if not self._last_args:
            return
        args = self._last_args
        top = tk.Toplevel(self.wrapper)
        top.title(args[2])
        top.geometry("1000x600")

        fig = Figure(figsize=(10, 6), dpi=100)
        _draw(fig.add_subplot(111), *args, font=10)
        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=top)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()

        bar = tk.Frame(top)
        bar.pack(fill="x")
        tk.Button(bar, text="Save PNG as...",
                  command=lambda: self.on_save_as(fig, args[2])).pack(side="left", padx=4, pady=4)

    @staticmethod
    def _save_as(fig, title, **dialog):
        dialog.setdefault("initialfile", f"{_safe_name(title)}.png")
        path = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG image", "*.png")], **dialog)
        if path:
            fig.savefig(path, dpi=150, bbox_inches="tight")
        return path

    def destroy(self):
        self.wrapper.destroy()


def save_chart_png(fig, chart_id: str, folder: str, base: str, dpi: int, log_cb=None) -> str:
    path = os.path.join(folder, f"{base}_{_safe_name(chart_id)}.png")
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    if log_cb:
        log_cb(f"Saved chart image: {path}")
    return path
