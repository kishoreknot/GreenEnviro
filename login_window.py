"""
login_window.py – Login dialog for H2 Dashboard
Displayed as a CTkToplevel on the hidden root window before device scanning.
"""

import os
import threading
import smtplib
import ssl
import customtkinter as ctk
from typing import Callable, Optional
from email.message import EmailMessage

# ── Shared visual constants (mirrors modern_dashboard.py) ────────────────────
_FONT      = "Inter"
BG_APP     = "#EEF2FF"       # app background (lavender)
BG_CARD    = "#FFFFFF"
BG_HEADER  = "#FFFFFF"
CLR_TITLE  = "#1E293B"
CLR_LABEL  = "#64748B"
CLR_ACCENT = "#0369A1"
CLR_DANGER = "#EF4444"
CLR_SAFE   = "#16A34A"

_W, _H = 440, 520            # login window size


def _logo_image(size: int = 64) -> Optional[ctk.CTkImage]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "green-logo.ico")
    try:
        from PIL import Image
        img = Image.open(path).convert("RGBA").resize((size, size))
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    except Exception:
        return None


class LoginWindow(ctk.CTkToplevel):
    """
    Blocking-style login dialog.

    Parameters
    ----------
    master      : parent Tk/CTk window (hidden root)
    on_success  : callable(user: auth.User) – fired when credentials are valid
    """

    def __init__(self, master, on_success: Callable):
        super().__init__(master)
        self._on_success = on_success
        self._build()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self):
        # Import here so auth.py can evolve independently
        from auth import init_auth_db
        init_auth_db()

        # Window chrome
        self.title("H2 Dashboard – Login")
        self.resizable(False, False)
        self.configure(fg_color=BG_APP)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Hide, position, then reveal (no top-left flash) ──────────────
        self.withdraw()
        self.update_idletasks()

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x  = (sw - _W) // 2
        y  = (sh - _H) // 2
        self.geometry(f"{_W}x{_H}+{x}+{y}")
        self.deiconify()
        self.lift()
        self.focus_force()

        # ── Card ─────────────────────────────────────────────────────────
        card = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=20,
                            border_width=0)
        card.place(relx=0.5, rely=0.5, anchor="center",
                   relwidth=0.88, relheight=0.90)

        # Logo
        logo = _logo_image(64)
        if logo:
            ctk.CTkLabel(card, text="", image=logo,
                         fg_color="transparent").pack(pady=(32, 0))
        else:
            ctk.CTkFrame(card, fg_color="transparent",
                         height=32).pack()

        # Title
        ctk.CTkLabel(card, text="H2 Detector Dashboard",
                     font=(_FONT, 18, "bold"),
                     text_color=CLR_TITLE,
                     fg_color="transparent").pack(pady=(10, 2))
        ctk.CTkLabel(card, text="Sign in to continue",
                     font=(_FONT, 11),
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack()

        # Divider
        ctk.CTkFrame(card, fg_color="#E2E8F0", height=1,
                     corner_radius=0).pack(fill="x", padx=28, pady=(20, 0))

        # ── Form ─────────────────────────────────────────────────────────
        form = ctk.CTkFrame(card, fg_color="transparent")
        form.pack(fill="x", padx=28, pady=(22, 0))

        # Username
        ctk.CTkLabel(form, text="Username",
                     font=(_FONT, 11, "bold"),
                     text_color=CLR_TITLE,
                     fg_color="transparent",
                     anchor="w").pack(fill="x")
        self._user_entry = ctk.CTkEntry(
            form,
            placeholder_text="Enter username",
            height=40, corner_radius=8,
            font=(_FONT, 12),
            border_color="#CBD5E1",
        )
        self._user_entry.pack(fill="x", pady=(4, 14))

        # Password
        ctk.CTkLabel(form, text="Password",
                     font=(_FONT, 11, "bold"),
                     text_color=CLR_TITLE,
                     fg_color="transparent",
                     anchor="w").pack(fill="x")
        self._pass_entry = ctk.CTkEntry(
            form,
            placeholder_text="Enter password",
            show="●",
            height=40, corner_radius=8,
            font=(_FONT, 12),
            border_color="#CBD5E1",
        )
        self._pass_entry.pack(fill="x", pady=(4, 4))

        # Show password + Forgot Password in one row to keep Sign In visible
        action_row = ctk.CTkFrame(form, fg_color="transparent")
        action_row.pack(fill="x", pady=(2, 0))

        self._show_pw = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            action_row,
            text="Show password",
            variable=self._show_pw,
            command=self._toggle_pw,
            font=(_FONT, 10),
            text_color=CLR_LABEL,
            fg_color=CLR_ACCENT,
            hover_color="#075985",
            checkmark_color="#FFFFFF",
            corner_radius=4,
        ).pack(side="left")

        self._forgot_btn = ctk.CTkButton(
            action_row,
            text="Forgot Password",
            height=28,
            width=120,
            corner_radius=8,
            font=(_FONT, 10, "bold"),
            fg_color="#E2E8F0",
            text_color=CLR_TITLE,
            hover_color="#CBD5E1",
            command=self._forgot_password,
        )
        self._forgot_btn.pack(side="right")

        # Error label
        self._err_lbl = ctk.CTkLabel(
            form, text="",
            font=(_FONT, 10),
            text_color=CLR_DANGER,
            fg_color="transparent",
            anchor="w",
        )
        self._err_lbl.pack(fill="x", pady=(8, 0))

        # Login button
        self._login_btn = ctk.CTkButton(
            form,
            text="Sign In",
            height=42,
            corner_radius=10,
            font=(_FONT, 13, "bold"),
            fg_color=CLR_ACCENT,
            hover_color="#075985",
            command=self._attempt_login,
        )
        self._login_btn.pack(fill="x", pady=(4, 0))

        # Footer hint
        ctk.CTkLabel(card,
                     text="Default: admin / Admin@123",
                     font=(_FONT, 9),
                     text_color=CLR_LABEL,
                     fg_color="transparent").pack(pady=(14, 0))

        # Bind Enter key
        self.bind("<Return>", lambda _e: self._attempt_login())
        self._user_entry.focus_set()

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _toggle_pw(self):
        self._pass_entry.configure(
            show="" if self._show_pw.get() else "●")

    def _toast(self, message: str, ok: bool = True):
        toast = ctk.CTkToplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        bg = "#DCFCE7" if ok else "#FEE2E2"
        fg = "#166534" if ok else "#991B1B"
        frame = ctk.CTkFrame(toast, fg_color=bg, corner_radius=10, border_width=1, border_color="#CBD5E1")
        frame.pack(fill="both", expand=True)
        ctk.CTkLabel(frame, text=message, font=(_FONT, 10, "bold"), text_color=fg, fg_color="transparent").pack(
            padx=12, pady=8
        )

        self.update_idletasks()
        toast.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - toast.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + self.winfo_height() - toast.winfo_reqheight() - 20
        toast.geometry(f"+{x}+{y}")
        toast.after(2600, lambda: toast.destroy() if toast.winfo_exists() else None)

    def _forgot_password(self):
        self._err_lbl.configure(text="")
        username_for_reset = self._ask_username_for_reset()
        if not username_for_reset:
            return
        self._forgot_btn.configure(state="disabled", text="Sending…")

        def _worker():
            try:
                from alert_manager import get_smtp_config
                from auth import reset_user_password_for_recovery

                cfg = get_smtp_config()
                host = str(cfg.get("host", "") or "").strip()
                username = str(cfg.get("username", "") or "").strip()
                from_email = str(cfg.get("from_email", "") or "").strip()
                recipient = from_email or username
                if not host or not recipient:
                    self.after(0, lambda: self._forgot_failed("SMTP is not configured."))
                    return

                port = int(cfg.get("port", 587) or 587)
                sec = int(cfg.get("use_tls", 1) or 1)
                smtp_user = username
                smtp_pwd = "".join(str(cfg.get("password", "") or "").split())

                ctx = ssl.create_default_context()
                if sec == 2:
                    server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=15)
                    server.ehlo()
                elif sec == 1:
                    server = smtplib.SMTP(host, port, timeout=15)
                    server.ehlo()
                    server.starttls(context=ctx)
                    server.ehlo()
                else:
                    server = smtplib.SMTP(host, port, timeout=15)
                    server.ehlo()

                if smtp_user and smtp_pwd:
                    server.login(smtp_user, smtp_pwd)

                ok, msg, temp_password, role = reset_user_password_for_recovery(username_for_reset)
                if not ok or not temp_password:
                    server.quit()
                    self.after(0, lambda m=msg: self._forgot_failed(m or "Password reset failed."))
                    return

                mail = EmailMessage()
                mail["From"] = recipient
                mail["To"] = recipient
                mail["Subject"] = "H2 Dashboard - Password Recovery"
                mail.set_content(
                    "User password has been reset for recovery.\n\n"
                    f"Username: {username_for_reset}\n"
                    f"Role: {role or 'unknown'}\n"
                    f"Temporary Password: {temp_password}\n\n"
                    "Please login and change this password immediately."
                )
                server.send_message(mail)
                server.quit()

                self.after(0, lambda r=recipient, rl=role: self._forgot_success(r, rl))
            except Exception as e:
                self.after(0, lambda m=str(e): self._forgot_failed(m))

        threading.Thread(target=_worker, daemon=True).start()

    def _ask_username_for_reset(self) -> str:
        popup = ctk.CTkToplevel(self)
        popup.title("Forgot Password")
        popup.resizable(False, False)
        popup.attributes("-topmost", True)
        popup.transient(self)

        card = ctk.CTkFrame(popup, fg_color=BG_CARD, corner_radius=10)
        card.pack(fill="both", expand=True, padx=10, pady=10)

        ctk.CTkLabel(
            card,
            text="Enter username to reset password",
            font=(_FONT, 11, "bold"),
            text_color=CLR_TITLE,
            fg_color="transparent",
        ).pack(anchor="w", padx=10, pady=(10, 6))

        entry = ctk.CTkEntry(card, width=260, height=34, font=(_FONT, 11))
        entry.pack(fill="x", padx=10, pady=(0, 10))
        entry.insert(0, self._user_entry.get().strip())
        entry.focus_set()

        result = {"value": ""}

        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(0, 10))

        def _submit():
            result["value"] = entry.get().strip()
            popup.destroy()

        def _cancel():
            popup.destroy()

        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=90,
            height=30,
            fg_color="#E2E8F0",
            text_color=CLR_TITLE,
            hover_color="#CBD5E1",
            command=_cancel,
        ).pack(side="right")
        ctk.CTkButton(
            btn_row,
            text="Reset",
            width=90,
            height=30,
            fg_color=CLR_ACCENT,
            hover_color="#075985",
            command=_submit,
        ).pack(side="right", padx=(0, 8))

        popup.bind("<Return>", lambda _e: _submit())
        popup.bind("<Escape>", lambda _e: _cancel())

        self.update_idletasks()
        popup.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - popup.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - popup.winfo_reqheight()) // 2
        popup.geometry(f"+{x}+{y}")

        popup.grab_set()
        self.wait_window(popup)
        return result["value"]

    def _forgot_success(self, recipient: str, role: str):
        self._forgot_btn.configure(state="normal", text="Forgot Password")
        if str(role or "").lower() == "operator":
            self._toast(
                "Password reset is done. Please reach out to Admin to get latest password.",
                ok=True,
            )
            return
        self._toast(f"Password has been sent to {recipient}", ok=True)

    def _forgot_failed(self, message: str):
        self._forgot_btn.configure(state="normal", text="Forgot Password")
        self._toast(message[:100], ok=False)

    def _attempt_login(self):
        from auth import authenticate
        username = self._user_entry.get().strip()
        password = self._pass_entry.get()

        self._err_lbl.configure(text="")
        self._login_btn.configure(state="disabled", text="Signing in…")
        self.update_idletasks()

        user = authenticate(username, password)

        self._login_btn.configure(state="normal", text="Sign In")

        if user is None:
            self._err_lbl.configure(
                text="⚠  Invalid username or password.")
            self._pass_entry.delete(0, "end")
            self._pass_entry.focus_set()
            return

        # Success — hand off to callback, then close this window
        self.withdraw()
        self._on_success(user)
        self.destroy()

    def _on_close(self):
        """Closing the login window exits the whole application."""
        self.master.destroy()
