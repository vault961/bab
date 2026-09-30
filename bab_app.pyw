import threading
import tkinter as tk
import traceback
from tkinter import messagebox

try:
    import pystray
    from PIL import Image, ImageDraw
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    import bab
except BaseException as error:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "밥 추천 봇 시작 실패",
        f"앱을 시작할 수 없습니다.\n\n{error}",
    )
    root.destroy()
    raise SystemExit(1) from error


class BabTrayApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("밥 추천 봇")
        self.root.geometry("380x190")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.bind("<Unmap>", self._on_unmap)

        self.status_var = tk.StringVar(value="Slack 연결 중...")
        self.detail_var = tk.StringVar(value="창을 최소화하면 트레이 아이콘으로 숨겨집니다.")
        self._closing = False
        self._hiding = False
        self.handler = None
        self.bot_thread = None

        self._build_window()
        self.icon = self._build_tray_icon()

        self.tray_thread = threading.Thread(
            target=self._run_tray_icon,
            name="bab-tray-icon",
            daemon=True,
        )
        self.tray_thread.start()
        self._start_bot()

    def _build_window(self) -> None:
        frame = tk.Frame(self.root, padx=24, pady=20)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text="밥 추천 봇",
            font=("맑은 고딕", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            frame,
            textvariable=self.status_var,
            font=("맑은 고딕", 11),
            pady=10,
        ).pack(anchor="w")
        tk.Label(
            frame,
            textvariable=self.detail_var,
            fg="#666666",
            wraplength=330,
            justify="left",
        ).pack(anchor="w")

        buttons = tk.Frame(frame, pady=15)
        buttons.pack(anchor="e")
        tk.Button(buttons, text="최소화", command=self.minimize_to_tray).pack(
            side="left", padx=(0, 8)
        )
        tk.Button(buttons, text="종료", command=self.request_exit).pack(side="left")

    @staticmethod
    def _create_icon_image():
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((3, 3, 61, 61), fill="#f6c344", outline="#8c6410", width=2)
        draw.ellipse((14, 27, 50, 49), fill="#ffffff", outline="#6b4a2b", width=2)
        draw.arc((14, 18, 50, 43), start=180, end=360, fill="#6b4a2b", width=3)
        draw.line((20, 14, 14, 5), fill="#6b4a2b", width=3)
        draw.line((29, 14, 29, 4), fill="#6b4a2b", width=3)
        return image

    def _build_tray_icon(self):
        menu = pystray.Menu(
            pystray.MenuItem(
                "창 열기",
                lambda icon, item: self.show_window(),
                default=True,
            ),
            pystray.MenuItem("종료", lambda icon, item: self.request_exit()),
        )
        return pystray.Icon(
            "bab-server",
            self._create_icon_image(),
            "밥 추천 봇",
            menu,
        )

    def _run_tray_icon(self) -> None:
        self.icon.run()

    def _start_bot(self) -> None:
        self.bot_thread = threading.Thread(
            target=self._run_bot,
            name="bab-slack-bot",
            daemon=True,
        )
        self.bot_thread.start()

    def _run_bot(self) -> None:
        try:
            self.handler = SocketModeHandler(bab.app, bab.SLACK_APP_TOKEN)
            bab.start_poll_scheduler()
            self._call_on_ui(self._set_running)
            self.handler.start()
            if not self._closing:
                self._call_on_ui(
                    lambda: self._set_status(
                        "중지됨", "트레이 아이콘 메뉴에서 앱을 종료할 수 있습니다."
                    )
                )
        except BaseException as error:
            details = traceback.format_exc()
            self._call_on_ui(lambda: self._set_error(str(error), details))

    def _call_on_ui(self, callback) -> None:
        if not self._closing:
            try:
                self.root.after(0, callback)
            except tk.TclError:
                pass

    def _set_running(self) -> None:
        self._set_status("실행 중", "Slack Socket Mode에 연결되었습니다.")

    def _set_status(self, status: str, detail: str) -> None:
        self.status_var.set(status)
        self.detail_var.set(detail)

    def _set_error(self, error: str, details: str) -> None:
        self._set_status("오류", error)
        self.show_window()
        print(details)

    def _on_unmap(self, _event) -> None:
        if not self._hiding and self.root.state() == "iconic":
            self.root.after_idle(self.hide_to_tray)

    def minimize_to_tray(self) -> None:
        if self._closing:
            return
        try:
            self.root.iconify()
        except tk.TclError:
            self.hide_to_tray()

    def hide_to_tray(self) -> None:
        if self._closing or self._hiding:
            return
        self._hiding = True
        try:
            self.root.withdraw()
        finally:
            self._hiding = False

    def show_window(self) -> None:
        if self._closing:
            return
        try:
            self.root.after(0, self._show_window_on_ui)
        except tk.TclError:
            pass

    def _show_window_on_ui(self) -> None:
        if self._closing:
            return
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def request_exit(self) -> None:
        if self._closing:
            return
        self._closing = True

        try:
            self.root.after(0, self._finish_exit)
        except tk.TclError:
            pass

    def _finish_exit(self) -> None:
        self._set_status("종료 중", "잠시만 기다려 주세요.")

        bab.stop_poll_scheduler()

        if self.handler is not None:
            try:
                self.handler.close()
            except Exception:
                pass

        try:
            self.icon.stop()
        except Exception:
            pass
        self.root.after(100, self.root.destroy)


def main() -> None:
    root = tk.Tk()
    BabTrayApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
