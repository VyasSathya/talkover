"""The panel's window on macOS and Linux: the same pixels the Windows layered window shows, in a Tk window.

voice_panel.py draws every frame into `pix` (premultiplied BGRA, top-down, the Windows layout) and calls push().
This class shows those pixels in a borderless, always-on-top Tk window and feeds mouse events back to the panel
(on_move / on_down / on_up / on_leave, with msg_pt = the cursor in screen coordinates, as on Windows).

macOS: the window is truly transparent (Tk's systemTransparent), so the rounded panel floats like on Windows.
Linux: Tk has no per-pixel alpha, so the panel is composited over a dark backdrop the color of its edge.
Tk wants the main thread, so the panel's frame loop runs on Tk's after() timer instead of a Win32 message pump.
"""
import queue
import sys
import time

import numpy as np

IS_MAC = sys.platform == "darwin"
LINUX_BACKDROP = (22, 22, 26)


class TkWindow:
    hwnd = None

    def __init__(self, handler):
        import tkinter as tk
        from PIL import Image, ImageTk
        self._Image, self._ImageTk = Image, ImageTk
        self.handler = handler
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Talkover")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        if IS_MAC:
            try:
                self.root.attributes("-transparent", True)
                bg = "systemTransparent"
            except tk.TclError:
                bg = "#16161a"
        else:
            bg = "#%02x%02x%02x" % LINUX_BACKDROP
        self.root.configure(bg=bg)
        self.label = tk.Label(self.root, bd=0, highlightthickness=0, bg=bg, cursor="hand2")
        self.label.pack()
        self.label.bind("<Motion>", self._motion)
        self.label.bind("<ButtonPress-1>", self._down)
        self.label.bind("<ButtonRelease-1>", self._up)
        self.label.bind("<B1-Motion>", self._motion)
        self.label.bind("<Leave>", lambda e: self.handler.on_leave())
        self.w = self.h = 0
        self.pix = None
        self._photo = None
        self._shown = False

    # ---- the interface voice_panel.Panel uses (same as the Windows LayeredWindow)
    def dpi(self) -> int:
        return 96

    def resize(self, w: int, h: int) -> np.ndarray:
        self.w, self.h = int(w), int(h)
        self.pix = np.zeros((self.h, self.w, 4), np.uint8)
        x, y = self.pos() if self._shown else (0, 0)
        self.root.geometry("%dx%d+%d+%d" % (self.w, self.h, x, y))
        return self.pix

    def push(self) -> None:
        if self.pix is None:
            return
        bgra = self.pix.astype(np.float32)
        a = bgra[..., 3:4]
        rgb = bgra[..., [2, 1, 0]]
        if IS_MAC:
            straight = np.where(a > 0, rgb * 255.0 / np.maximum(a, 1.0), 0.0)
            rgba = np.concatenate([np.clip(straight, 0, 255), a], axis=-1).astype(np.uint8)
            img = self._Image.fromarray(rgba, "RGBA")
        else:
            back = np.asarray(LINUX_BACKDROP, np.float32)
            out = rgb + back * (1.0 - a / 255.0)        # premultiplied "over" the backdrop
            img = self._Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")
        self._photo = self._ImageTk.PhotoImage(img)
        self.label.configure(image=self._photo)

    def move(self, x: int, y: int) -> None:
        self.root.geometry("+%d+%d" % (int(x), int(y)))

    def pos(self):
        self.root.update_idletasks()
        return self.root.winfo_x(), self.root.winfo_y()

    def keep_on_top(self) -> None:
        self.root.attributes("-topmost", True)
        self.root.lift()

    def show(self, on: bool) -> None:
        if on:
            self.root.deiconify()
            self._shown = True
            self.keep_on_top()
        else:
            self.root.withdraw()

    def screen_size(self):
        return self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def destroy(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---- mouse
    def _motion(self, e):
        self.handler.msg_pt = (e.x_root, e.y_root)
        self.handler.on_move(e.x, e.y)

    def _down(self, e):
        self.handler.msg_pt = (e.x_root, e.y_root)
        self.handler.on_down(e.x, e.y)

    def _up(self, e):
        self.handler.msg_pt = (e.x_root, e.y_root)
        self.handler.on_up(e.x, e.y)

    # ---- the frame loop (Tk's timer instead of the Win32 message pump)
    def run(self, panel) -> None:
        state = {"last": time.perf_counter()}

        def tick():
            if not panel.running:
                self.destroy()
                return
            while True:
                try:
                    fn = panel.actions.get_nowait()
                except queue.Empty:
                    break
                panel.safe(fn)
            now = time.perf_counter()
            dt = min(0.1, now - state["last"])
            state["last"] = now
            if panel.visible:
                panel.safe(lambda: panel.frame(dt))
                busy = panel.mode in ("speak", "listen", "think") or panel.hover or panel.drag
                settled = panel.mode == "off" and panel.alpha.v < 0.71
                fps = 60 if busy else (8 if settled else 30)
            else:
                panel.safe(panel.poll)
                fps = 4
            self.root.after(max(2, int(1000 / fps)), tick)

        self.root.after(10, tick)
        self.root.mainloop()
