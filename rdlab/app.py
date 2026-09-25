"""rdlab - l'application.

    python -m rdlab.app

Une seule fenetre pour les deux roles : partager son ecran, ou se
connecter a une autre machine.

POURQUOI UNE APPLICATION ET PAS JUSTE DES SCRIPTS

Les decisions de securite de ce logiciel sont prises par un HUMAIN :
accorder une session, verifier une empreinte, activer l'acces sans
surveillance. En ligne de commande, ces decisions se prennent dans un
terminal que personne ne regarde. Ici, elles sont des boites de dialogue
modales, avec le defaut sur "refuser" et un compte a rebours visible.

La regle de non-furtivite est appliquee dans l'interface elle-meme :
quand une session est active, la banniere passe en rouge et reste
visible. On ne peut pas partager son ecran sans le voir.

ARCHITECTURE

    [fil interface tkinter]        <- le SEUL qui touche aux widgets
            |  after(0, ...)
    [fil moteur hote]  HostService.start()
    [fil connexion]    client.connect()

Les fils de fond ne manipulent jamais un widget : ils passent par
_ui(), qui reporte l'appel sur la boucle tkinter. Les demandes qui
exigent une reponse humaine (consentement, empreinte, mot de passe)
bloquent le fil de fond sur un threading.Event pendant que l'interface
affiche la boite de dialogue.
"""

import socket
import threading
import time
import tkinter as tk
from tkinter import ttk

from . import auth, client, handshake, host, identity, session_log

APP_NAME = "rdlab"
CONSENT_TIMEOUT = 60

BG = "#12121a"
CARD = "#1b1b26"
EDGE = "#2a2a3a"
FG = "#e6e6f0"
MUTED = "#8b8ba7"
ACCENT = "#7aa2f7"
OK = "#9ece6a"
WARN = "#e0af68"
BAD = "#f7768e"

F_TITLE = ("Segoe UI Semibold", 13)
F_BODY = ("Segoe UI", 10)
F_SMALL = ("Segoe UI", 9)
F_MONO = ("Consolas", 10)
F_ID = ("Consolas", 26, "bold")
F_SAS = ("Consolas", 30, "bold")


# =====================================================================
#  Petits composants
# =====================================================================

def card(parent, title=None):
    frame = tk.Frame(parent, bg=CARD, highlightbackground=EDGE,
                     highlightthickness=1)
    if title:
        tk.Label(frame, text=title, bg=CARD, fg=ACCENT, font=F_TITLE,
                 anchor="w").pack(fill="x", padx=14, pady=(12, 6))
    return frame


def label(parent, text, fg=FG, font=F_BODY, **kw):
    return tk.Label(parent, text=text, bg=kw.pop("bg", CARD), fg=fg,
                    font=font, anchor="w", justify="left", **kw)


def entry(parent, textvariable, width=24, show=None):
    return tk.Entry(parent, textvariable=textvariable, width=width, show=show,
                    bg=BG, fg=FG, insertbackground=FG, relief="flat",
                    font=F_BODY, highlightbackground=EDGE,
                    highlightthickness=1)


def button(parent, text, command, kind="normal", width=None):
    colors = {"normal": (EDGE, FG), "primary": (ACCENT, "#0d0d12"),
              "danger": (BAD, "#160d10"), "ok": (OK, "#101408")}
    bg, fg = colors[kind]
    return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                     activebackground=bg, activeforeground=fg, relief="flat",
                     font=F_BODY, padx=14, pady=7, cursor="hand2",
                     width=width, borderwidth=0,
                     disabledforeground=MUTED)


class ScrollFrame(tk.Frame):
    """Cadre defilant. L'onglet Partage depasse la hauteur d'un ecran de
    portable (1366x768) : sans defilement, le bouton de demarrage se
    retrouve sous la ligne de flottaison et l'application parait cassee."""

    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self._canvas = tk.Canvas(self, bg=BG, highlightthickness=0,
                                 borderwidth=0)
        bar = ttk.Scrollbar(self, orient="vertical",
                            command=self._canvas.yview)
        self.body = tk.Frame(self._canvas, bg=BG)
        self._win = self._canvas.create_window((0, 0), window=self.body,
                                               anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self._canvas.bind("<Configure>", self._on_canvas)
        self._canvas.configure(yscrollcommand=bar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for w in (self._canvas, self.body):
            w.bind("<Enter>", lambda e: self._canvas.bind_all(
                "<MouseWheel>", self._on_wheel))
            w.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

    def _on_body(self, _event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, event):
        self._canvas.itemconfigure(self._win, width=event.width)

    def _on_wheel(self, event):
        self._canvas.yview_scroll(-1 * (event.delta // 120), "units")


class ConsoleBox(tk.Text):
    """Zone de journal en lecture seule, avec couleurs par niveau."""

    def __init__(self, parent, height=10):
        super().__init__(parent, height=height, bg=BG, fg=MUTED, relief="flat",
                         font=("Consolas", 9), wrap="word", padx=10, pady=8,
                         highlightbackground=EDGE, highlightthickness=1)
        self.tag_config("info", foreground=MUTED)
        self.tag_config("good", foreground=OK)
        self.tag_config("warn", foreground=WARN)
        self.tag_config("bad", foreground=BAD)
        self.tag_config("accent", foreground=ACCENT)
        self.configure(state="disabled")

    def write(self, message, tag="info"):
        self.configure(state="normal")
        self.insert("end", "%s  %s\n" % (time.strftime("%H:%M:%S"), message),
                    tag)
        self.see("end")
        self.configure(state="disabled")

    def clear(self):
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")


# =====================================================================
#  Boites de dialogue qui BLOQUENT un fil de fond
# =====================================================================

class ModalAnswer:
    """Transporte une reponse humaine vers le fil qui l'attend."""

    def __init__(self, default=False):
        self.value = default
        self.extra = None
        self.done = threading.Event()

    def set(self, value, extra=None):
        self.value = value
        self.extra = extra
        self.done.set()

    def wait(self, timeout):
        self.done.wait(timeout)
        return self.value


def _modal(root, title, width=460):
    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg=CARD)
    win.transient(root)
    win.resizable(False, False)
    win.grab_set()
    root.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - width) // 2
    y = root.winfo_rooty() + 120
    win.geometry("%dx%d+%d+%d" % (width, 10, max(0, x), max(0, y)))
    return win


def consent_dialog(root, request, answer):
    """Section 10 : consentement explicite, defaut = refus, expiration = refus."""
    win = _modal(root, "Demande de connexion")
    win.protocol("WM_DELETE_WINDOW", lambda: _close(False))

    tk.Label(win, text="Une machine demande a voir votre ecran", bg=CARD,
             fg=WARN, font=F_TITLE).pack(padx=20, pady=(18, 10))
    info = tk.Frame(win, bg=CARD)
    info.pack(fill="x", padx=20)
    for k, v in (("Client", request["client"]), ("Adresse", request["peer"]),
                 ("Permissions", "observation seule"
                  if request.get("view_only") else "controle clavier/souris")):
        row = tk.Frame(info, bg=CARD)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=k, bg=CARD, fg=MUTED, font=F_SMALL,
                 width=12, anchor="w").pack(side="left")
        tk.Label(row, text=v, bg=CARD, fg=FG, font=F_BODY,
                 anchor="w").pack(side="left")

    tk.Label(win, text="Code de verification", bg=CARD, fg=MUTED,
             font=F_SMALL).pack(pady=(14, 0))
    tk.Label(win, text=request["sas"], bg=CARD, fg=ACCENT,
             font=F_SAS).pack()
    tk.Label(win, text="Il doit etre IDENTIQUE a celui affiche chez le client.\n"
                       "S'ils different, quelqu'un s'interpose : refusez.",
             bg=CARD, fg=MUTED, font=F_SMALL, justify="center").pack(pady=(2, 12))

    countdown = tk.StringVar()
    tk.Label(win, textvariable=countdown, bg=CARD, fg=MUTED,
             font=F_SMALL).pack()

    row = tk.Frame(win, bg=CARD)
    row.pack(pady=(10, 18))

    def _close(value):
        if not answer.done.is_set():
            answer.set(value)
        try:
            win.grab_release()
            win.destroy()
        except tk.TclError:
            pass

    button(row, "Refuser", lambda: _close(False), "danger",
           width=12).pack(side="left", padx=6)
    button(row, "Autoriser", lambda: _close(True), "ok",
           width=12).pack(side="left", padx=6)

    deadline = time.monotonic() + CONSENT_TIMEOUT

    def tick():
        left = deadline - time.monotonic()
        if left <= 0:
            countdown.set("expire - refus automatique")
            _close(False)
            return
        countdown.set("refus automatique dans %d s" % int(left))
        win.after(500, tick)

    tick()
    win.update_idletasks()
    win.geometry("460x%d" % win.winfo_reqheight())
    return win


def verify_dialog(root, core, status, sas, answer):
    """Verification de l'identite de l'hote, cote client (TOFU)."""
    win = _modal(root, "Verifier l'hote")
    win.protocol("WM_DELETE_WINDOW", lambda: _close(False))

    heads = {"new": ("Premiere connexion a cette machine", WARN),
             "match": ("Machine deja connue, empreinte inchangee", OK),
             "mismatch": ("ALERTE : l'empreinte a change", BAD)}
    text, color = heads.get(status, heads["new"])
    tk.Label(win, text=text, bg=CARD, fg=color, font=F_TITLE).pack(
        padx=20, pady=(18, 10))

    if status == "mismatch":
        tk.Label(win, text="Cet identifiant etait associe a une AUTRE cle.\n"
                           "Reinstallation de l'hote... ou interception.\n"
                           "En cas de doute : refusez.",
                 bg=CARD, fg=BAD, font=F_SMALL, justify="center").pack(pady=4)

    body = tk.Frame(win, bg=CARD)
    body.pack(fill="x", padx=20)
    tk.Label(body, text="Identifiant", bg=CARD, fg=MUTED,
             font=F_SMALL, anchor="w").pack(fill="x")
    tk.Label(body, text=core["machine_id"], bg=CARD, fg=FG,
             font=F_MONO, anchor="w").pack(fill="x")
    tk.Label(body, text="Empreinte", bg=CARD, fg=MUTED, font=F_SMALL,
             anchor="w").pack(fill="x", pady=(8, 0))
    tk.Label(body, text=core["fingerprint"], bg=CARD, fg=FG, font=("Consolas", 9),
             anchor="w", wraplength=400, justify="left").pack(fill="x")

    tk.Label(win, text="Code de verification", bg=CARD, fg=MUTED,
             font=F_SMALL).pack(pady=(14, 0))
    tk.Label(win, text=sas, bg=CARD, fg=ACCENT, font=F_SAS).pack()
    tk.Label(win, text="Comparez empreinte et code avec l'ecran de l'hote.",
             bg=CARD, fg=MUTED, font=F_SMALL).pack(pady=(2, 12))

    row = tk.Frame(win, bg=CARD)
    row.pack(pady=(6, 18))

    def _close(value):
        if not answer.done.is_set():
            answer.set(value)
        try:
            win.grab_release()
            win.destroy()
        except tk.TclError:
            pass

    button(row, "Annuler", lambda: _close(False), "danger",
           width=12).pack(side="left", padx=6)
    button(row, "C'est bien l'hote", lambda: _close(True), "ok",
           width=16).pack(side="left", padx=6)
    win.update_idletasks()
    win.geometry("460x%d" % win.winfo_reqheight())
    return win


def password_dialog(root, answer, title="Mot de passe de l'hote",
                    confirm=False):
    win = _modal(root, title)
    win.protocol("WM_DELETE_WINDOW", lambda: _close(None))
    tk.Label(win, text=title, bg=CARD, fg=ACCENT, font=F_TITLE).pack(
        padx=20, pady=(18, 4))
    hint = ("10 caracteres minimum. Il protege l'acces a votre ecran :\n"
            "il doit etre different de votre mot de passe de session."
            if confirm else
            "Il ne quittera jamais cette machine : seule une preuve\n"
            "cryptographique est transmise.")
    tk.Label(win, text=hint, bg=CARD, fg=MUTED, font=F_SMALL,
             justify="center").pack(pady=(0, 12))

    v1 = tk.StringVar()
    v2 = tk.StringVar()
    e1 = entry(win, v1, width=32, show="*")
    e1.pack(pady=4)
    if confirm:
        entry(win, v2, width=32, show="*").pack(pady=4)
    err = tk.StringVar()
    tk.Label(win, textvariable=err, bg=CARD, fg=BAD, font=F_SMALL).pack()

    def _close(value):
        if not answer.done.is_set():
            answer.set(value)
        try:
            win.grab_release()
            win.destroy()
        except tk.TclError:
            pass

    def _ok():
        if confirm:
            if len(v1.get()) < 10:
                err.set("10 caracteres minimum")
                return
            if v1.get() != v2.get():
                err.set("les deux saisies different")
                return
        _close(v1.get())

    row = tk.Frame(win, bg=CARD)
    row.pack(pady=(12, 18))
    button(row, "Annuler", lambda: _close(None), "normal",
           width=10).pack(side="left", padx=6)
    button(row, "Valider", _ok, "primary", width=10).pack(side="left", padx=6)
    e1.bind("<Return>", lambda e: _ok())
    e1.focus_set()
    win.update_idletasks()
    win.geometry("420x%d" % win.winfo_reqheight())
    return win


# =====================================================================
#  Le pont moteur -> interface
# =====================================================================

class GuiHooks(host.HostHooks):
    """Traduit les evenements du moteur en actions sur l'interface.

    Aucun widget n'est touche ici : tout passe par app._ui(), qui
    reporte l'appel sur la boucle tkinter."""

    def __init__(self, app):
        self.app = app

    def status(self, message):
        self.app._ui(self.app.host_status.set, message)

    def event(self, kind, **fields):
        self.app._ui(self.app._on_host_event, kind, fields)

    def consent(self, request):
        answer = ModalAnswer(default=False)
        self.app._ui(consent_dialog, self.app.root, request, answer)
        return answer.wait(CONSENT_TIMEOUT + 5)


# =====================================================================
#  L'application
# =====================================================================

class RdlabApp:
    def __init__(self):
        self.device = identity.DeviceIdentity.load_or_create()
        self.creds = auth.CredentialStore()
        self.service = None
        self.service_thread = None
        self.viewer_ctrl = None
        self.session_active = False

        self.root = tk.Tk()
        self.root.title("%s - bureau a distance (laboratoire)" % APP_NAME)
        self.root.geometry("920x820")
        self.root.minsize(820, 560)
        self.root.configure(bg=BG)
        self._init_style()
        self._build()
        self._refresh_credentials()
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit)

    # -- utilitaires --------------------------------------------------
    def _ui(self, fn, *args):
        """Reporte un appel sur le fil de l'interface. Seul point de
        passage autorise depuis un fil de fond."""
        try:
            self.root.after(0, lambda: fn(*args))
        except RuntimeError:
            pass

    def _init_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=CARD, foreground=MUTED,
                        padding=(18, 9), font=F_BODY, borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", BG)],
                  foreground=[("selected", ACCENT)])
        style.configure("TCombobox", fieldbackground=BG, background=CARD,
                        foreground=FG, arrowcolor=FG)

    # -- construction -------------------------------------------------
    def _build(self):
        self.banner = tk.Frame(self.root, bg=CARD, height=54)
        self.banner.pack(fill="x")
        self.banner.pack_propagate(False)
        self.banner_text = tk.StringVar(
            value="Laboratoire pedagogique - reseau de test uniquement")
        self.banner_label = tk.Label(self.banner, textvariable=self.banner_text,
                                     bg=CARD, fg=MUTED, font=F_BODY,
                                     anchor="w")
        self.banner_label.pack(side="left", padx=18)
        tk.Label(self.banner, text=APP_NAME, bg=CARD, fg=ACCENT,
                 font=("Segoe UI Semibold", 16)).pack(side="right", padx=18)

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=14)
        share_scroll = ScrollFrame(nb)
        connect_scroll = ScrollFrame(nb)
        self.tab_share = share_scroll.body
        self.tab_connect = connect_scroll.body
        self.tab_log = tk.Frame(nb, bg=BG)
        nb.add(share_scroll, text="Partager mon ecran")
        nb.add(connect_scroll, text="Se connecter")
        nb.add(self.tab_log, text="Journal")
        nb.enable_traversal()          # Ctrl+Tab entre les onglets
        nb.bind("<<NotebookTabChanged>>", lambda e: self._refresh_log())
        self._build_share()
        self._build_connect()
        self._build_log()

    # ---------------------------------------------------------- PARTAGE
    def _build_share(self):
        root = self.tab_share
        top = card(root)
        top.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(top, bg=CARD)
        inner.pack(fill="x", padx=16, pady=14)
        label(inner, "Identifiant de cette machine", MUTED, F_SMALL).pack(
            fill="x")
        idrow = tk.Frame(inner, bg=CARD)
        idrow.pack(fill="x")
        tk.Label(idrow, text=self.device.machine_id, bg=CARD, fg=FG,
                 font=F_ID).pack(side="left")
        button(idrow, "Copier", self._copy_id).pack(side="left", padx=14)
        label(inner, "Empreinte - a verifier avec le client", MUTED,
              F_SMALL).pack(fill="x", pady=(10, 0))
        fp = tk.Frame(inner, bg=CARD)
        fp.pack(fill="x")
        tk.Label(fp, text=self.device.fingerprint, bg=CARD, fg=ACCENT,
                 font=("Consolas", 9)).pack(side="left")
        button(fp, "Copier", self._copy_fingerprint).pack(side="left", padx=14)

        # --- securite
        sec = card(root, "Securite")
        sec.pack(fill="x", pady=(0, 12))
        body = tk.Frame(sec, bg=CARD)
        body.pack(fill="x", padx=16, pady=(0, 14))

        self.pw_state = tk.StringVar()
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=3)
        self.pw_label = tk.Label(row, textvariable=self.pw_state, bg=CARD,
                                 fg=FG, font=F_BODY, anchor="w")
        self.pw_label.pack(side="left")
        button(row, "Definir le mot de passe",
               self._set_password).pack(side="right")

        self.unattended_var = tk.BooleanVar(value=self.creds.unattended)
        chk = tk.Checkbutton(
            body, text="Acces sans surveillance (accepter sans me demander)",
            variable=self.unattended_var, command=self._toggle_unattended,
            bg=CARD, fg=FG, selectcolor=BG, activebackground=CARD,
            activeforeground=FG, font=F_BODY, anchor="w",
            highlightthickness=0, borderwidth=0)
        chk.pack(fill="x", pady=(10, 0))
        label(body, "Desactive par defaut. Exige un mot de passe. Toutes les "
                    "sessions restent journalisees.", MUTED, F_SMALL).pack(
            fill="x", padx=24)

        self.viewonly_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            body, text="Observation seule (ne pas appliquer clavier/souris)",
            variable=self.viewonly_var, bg=CARD, fg=FG, selectcolor=BG,
            activebackground=CARD, activeforeground=FG, font=F_BODY,
            anchor="w", highlightthickness=0, borderwidth=0).pack(
            fill="x", pady=(10, 0))

        # --- mode reseau
        net = card(root, "Comment le client vous joint")
        net.pack(fill="x", pady=(0, 12))
        body = tk.Frame(net, bg=CARD)
        body.pack(fill="x", padx=16, pady=(0, 14))
        self.mode_var = tk.StringVar(value="relay")

        def radio(parent, text, value):
            return tk.Radiobutton(
                parent, text=text, variable=self.mode_var, value=value,
                command=self._refresh_mode, bg=CARD, fg=FG, selectcolor=BG,
                activebackground=CARD, activeforeground=FG, font=F_BODY,
                anchor="w", highlightthickness=0, borderwidth=0)

        radio(body, "Via une antenne (VPS) - aucun port a ouvrir", "relay"
              ).pack(fill="x")
        relay_row = tk.Frame(body, bg=CARD)
        relay_row.pack(fill="x", padx=24, pady=(2, 8))
        self.relay_var = tk.StringVar(value="")
        label(relay_row, "Adresse :", MUTED, F_SMALL).pack(side="left")
        self.relay_entry = entry(relay_row, self.relay_var, 34)
        self.relay_entry.pack(side="left", padx=8)
        label(relay_row, "ex. vps.exemple.net:7800", MUTED, F_SMALL).pack(
            side="left")

        radio(body, "Reseau local - ecouter un port sur cette machine",
              "direct").pack(fill="x")
        lan_row = tk.Frame(body, bg=CARD)
        lan_row.pack(fill="x", padx=24, pady=(2, 0))
        self.bind_var = tk.StringVar(value=self._guess_lan_ip())
        self.port_var = tk.StringVar(value="7700")
        label(lan_row, "Adresse :", MUTED, F_SMALL).pack(side="left")
        self.bind_entry = entry(lan_row, self.bind_var, 16)
        self.bind_entry.pack(side="left", padx=8)
        label(lan_row, "Port :", MUTED, F_SMALL).pack(side="left")
        self.port_entry = entry(lan_row, self.port_var, 7)
        self.port_entry.pack(side="left", padx=8)

        # --- controle
        ctrl = tk.Frame(root, bg=BG)
        ctrl.pack(fill="x", pady=(0, 10))
        self.share_btn = button(ctrl, "Demarrer le partage", self._toggle_share,
                                "primary", width=22)
        self.share_btn.pack(side="left")
        self.endsession_btn = button(ctrl, "Terminer la session",
                                     self._end_session, "normal", width=20)
        self.endsession_btn.pack(side="left", padx=10)
        self._set_endsession(False)
        self.host_status = tk.StringVar(value="a l arret")
        tk.Label(ctrl, textvariable=self.host_status, bg=BG, fg=MUTED,
                 font=F_BODY).pack(side="left", padx=14)

        self.host_console = ConsoleBox(root, height=8)
        self.host_console.pack(fill="x", pady=(0, 12))
        self._refresh_mode()

    # -------------------------------------------------------- CONNEXION
    def _build_connect(self):
        root = self.tab_connect
        c = card(root, "Machine a joindre")
        c.pack(fill="x", pady=(0, 12))
        body = tk.Frame(c, bg=CARD)
        body.pack(fill="x", padx=16, pady=(0, 14))

        self.cmode_var = tk.StringVar(value="relay")

        def radio(text, value):
            return tk.Radiobutton(
                body, text=text, variable=self.cmode_var, value=value,
                command=self._refresh_cmode, bg=CARD, fg=FG, selectcolor=BG,
                activebackground=CARD, activeforeground=FG, font=F_BODY,
                anchor="w", highlightthickness=0, borderwidth=0)

        radio("Via une antenne (VPS)", "relay").pack(fill="x")
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", padx=24, pady=(2, 8))
        self.cvia_var = tk.StringVar(value="")
        label(row, "Antenne :", MUTED, F_SMALL).pack(side="left")
        self.cvia_entry = entry(row, self.cvia_var, 34)
        self.cvia_entry.pack(side="left", padx=8)

        radio("Reseau local (adresse IP)", "direct").pack(fill="x")
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", padx=24, pady=(2, 8))
        self.cport_var = tk.StringVar(value="7700")
        label(row, "Port :", MUTED, F_SMALL).pack(side="left")
        self.cport_entry = entry(row, self.cport_var, 7)
        self.cport_entry.pack(side="left", padx=8)

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        self.target_var = tk.StringVar()
        self.target_label = label(row, "Identifiant (9 chiffres) :", MUTED,
                                  F_SMALL)
        self.target_label.pack(side="left")
        self.target_entry = entry(row, self.target_var, 22)
        self.target_entry.pack(side="left", padx=8)
        label(row, "Qualite :", MUTED, F_SMALL).pack(side="left", padx=(14, 0))
        self.quality_var = tk.StringVar(value="balanced")
        ttk.Combobox(row, textvariable=self.quality_var, width=10,
                     state="readonly",
                     values=["low", "balanced", "high"]).pack(side="left",
                                                              padx=8)

        ctrl = tk.Frame(root, bg=BG)
        ctrl.pack(fill="x", pady=(0, 10))
        self.connect_btn = button(ctrl, "Se connecter", self._do_connect,
                                  "primary", width=18)
        self.connect_btn.pack(side="left")
        self.client_status = tk.StringVar(value="pret")
        tk.Label(ctrl, textvariable=self.client_status, bg=BG, fg=MUTED,
                 font=F_BODY).pack(side="left", padx=14)

        self.client_console = ConsoleBox(root, height=12)
        self.client_console.pack(fill="x", pady=(0, 12))
        self._refresh_cmode()

    # ------------------------------------------------------------ LOG
    def _build_log(self):
        root = self.tab_log
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="Journal des connexions - rdlab-data/sessions.log",
                 bg=BG, fg=MUTED, font=F_BODY).pack(side="left")
        button(head, "Rafraichir", self._refresh_log).pack(side="right")
        self.log_box = ConsoleBox(root, height=24)
        self.log_box.pack(fill="both", expand=True)

    def _refresh_log(self):
        if not hasattr(self, "log_box"):
            return
        self.log_box.clear()
        entries = session_log.tail(300)
        if not entries:
            self.log_box.write("journal vide", "info")
            return
        tags = {"auth_failed": "bad", "handshake_failed": "bad",
                "session_error": "bad", "consent": "warn",
                "session_start": "good", "unattended_changed": "warn",
                "password_changed": "accent", "relay_registered": "accent"}
        for e in entries:
            rest = " ".join("%s=%s" % (k, v) for k, v in e.items()
                            if k not in ("ts", "event"))
            self.log_box.write("%s  %-20s %s" % (e["ts"], e["event"], rest),
                               tags.get(e["event"], "info"))

    # -- etat / rafraichissements -------------------------------------
    def _guess_lan_ip(self):
        """Adresse LAN probable. Aucun paquet n'est reellement emis :
        connect() sur UDP ne fait que choisir une route."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))     # reseau de documentation, RFC 5737
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    def _refresh_credentials(self):
        if self.creds.has_password:
            self.pw_state.set("Mot de passe : defini")
            self.pw_label.configure(fg=OK)
        else:
            self.pw_state.set("Mot de passe : AUCUN - partage impossible")
            self.pw_label.configure(fg=BAD)
        self.unattended_var.set(self.creds.unattended)

    def _refresh_mode(self):
        relay = self.mode_var.get() == "relay"
        for w in (self.relay_entry,):
            w.configure(state="normal" if relay else "disabled")
        for w in (self.bind_entry, self.port_entry):
            w.configure(state="disabled" if relay else "normal")

    def _refresh_cmode(self):
        relay = self.cmode_var.get() == "relay"
        self.cvia_entry.configure(state="normal" if relay else "disabled")
        self.cport_entry.configure(state="disabled" if relay else "normal")
        self.target_label.configure(
            text="Identifiant (9 chiffres) :" if relay else "Adresse IP :")

    def _set_endsession(self, active):
        """Un bouton grise garde sinon sa couleur d'alerte et laisse croire
        qu'une session est en cours."""
        if active:
            self.endsession_btn.configure(state="normal", bg=BAD,
                                          activebackground=BAD, fg="#160d10")
        else:
            self.endsession_btn.configure(state="disabled", bg=EDGE,
                                          activebackground=EDGE, fg=MUTED)

    def _set_banner(self, text, color, bg=CARD):
        self.banner_text.set(text)
        self.banner_label.configure(fg=color, bg=bg)
        self.banner.configure(bg=bg)
        for child in self.banner.winfo_children():
            child.configure(bg=bg)

    # -- actions : securite -------------------------------------------
    def _copy_id(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.device.machine_id)
        self.host_console.write("identifiant copie", "accent")

    def _copy_fingerprint(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.device.fingerprint)
        self.host_console.write("empreinte copiee", "accent")

    def _set_password(self):
        answer = ModalAnswer(default=None)
        win = password_dialog(self.root, answer,
                              "Definir le mot de passe d'acces", confirm=True)
        self.root.wait_window(win)      # on est sur le fil interface : on peut
        pw = answer.value
        if not pw:
            return
        try:
            self.creds.set_password(pw)
        except ValueError as exc:
            self.host_console.write(str(exc), "bad")
            return
        session_log.log("password_changed")
        self.host_console.write("mot de passe enregistre (scrypt N=32768)",
                                "good")
        self._refresh_credentials()

    def _toggle_unattended(self):
        try:
            if self.unattended_var.get():
                self.creds.enable_unattended()
                self.host_console.write(
                    "acces sans surveillance ACTIVE - les connexions "
                    "authentifiees seront acceptees sans vous demander", "warn")
            else:
                self.creds.disable_unattended()
                self.host_console.write("acces sans surveillance desactive",
                                        "good")
            session_log.log("unattended_changed",
                            value="on" if self.unattended_var.get() else "off")
        except ValueError as exc:
            self.host_console.write(str(exc), "bad")
        self._refresh_credentials()

    # -- actions : partage ---------------------------------------------
    def _toggle_share(self):
        if self.service:
            self._stop_share()
        else:
            self._start_share()

    def _start_share(self):
        if not self.creds.has_password:
            self.host_console.write(
                "definissez d'abord un mot de passe d'acces", "bad")
            return
        relay = None
        bind, port = "127.0.0.1", 7700
        if self.mode_var.get() == "relay":
            relay = self.relay_var.get().strip()
            if not relay:
                self.host_console.write("indiquez l'adresse de l'antenne",
                                        "bad")
                return
        else:
            bind = self.bind_var.get().strip() or "127.0.0.1"
            try:
                port = int(self.port_var.get())
            except ValueError:
                self.host_console.write("port invalide", "bad")
                return

        options = host.HostOptions(bind=bind, port=port, relay=relay,
                                   view_only=self.viewonly_var.get())
        self.service = host.HostService(self.device, self.creds, options,
                                        GuiHooks(self))
        self.service_thread = threading.Thread(target=self._run_service,
                                               daemon=True)
        self.service_thread.start()
        self.share_btn.configure(text="Arreter le partage", bg=BAD,
                                 activebackground=BAD, fg="#160d10")
        self._set_banner("PARTAGE ACTIF - cet ordinateur peut etre observe",
                         WARN)
        self.host_console.write(
            "partage demarre (%s)" % ("antenne %s" % relay if relay
                                      else "%s:%d" % (bind, port)), "good")

    def _run_service(self):
        try:
            self.service.start()
        except Exception as exc:
            self._ui(self.host_console.write, "moteur arrete : %s" % exc, "bad")
        finally:
            self._ui(self._on_service_stopped)

    def _stop_share(self):
        if self.service:
            self.service.stop()
        self.host_console.write("arret demande", "info")

    def _on_service_stopped(self):
        self.service = None
        self.session_active = False
        self.share_btn.configure(text="Demarrer le partage", bg=ACCENT,
                                 activebackground=ACCENT, fg="#0d0d12")
        self._set_endsession(False)
        self.host_status.set("a l arret")
        self._set_banner("Laboratoire pedagogique - reseau de test uniquement",
                         MUTED)

    def _end_session(self):
        if self.service:
            self.service.stop_session()

    def _on_host_event(self, kind, fields):
        if kind == "session_stats":
            q = fields["quality"]
            self.host_status.set(
                "session active - %d img/s cible, qualite %d, RTT %s ms, "
                "%d Ko envoyes" % (q["fps"], q["quality"], q["rtt_ms"],
                                   fields["kbytes"]))
            return
        if kind == "session_start":
            self.session_active = True
            self._set_endsession(True)
            mode = ("observation seule" if not fields["allow_input"]
                    else "controle clavier/souris")
            self._set_banner(
                "SESSION EN COURS - %s vous observe (%s)"
                % (fields["client"], mode), "#ffffff", BAD)
            self.host_console.write("session ouverte avec %s (%s)"
                                    % (fields["client"], mode), "good")
        elif kind == "session_end":
            self.session_active = False
            self._set_endsession(False)
            self._set_banner(
                "PARTAGE ACTIF - cet ordinateur peut etre observe", WARN)
            self.host_console.write("session terminee (%s s)"
                                    % fields["duration_s"], "info")
        elif kind == "auth_failed":
            self.host_console.write("mot de passe REFUSE pour %s"
                                    % fields.get("peer"), "bad")
        elif kind == "consent_denied":
            self.host_console.write("connexion refusee par vous", "warn")
        elif kind == "handshake_failed":
            self.host_console.write("poignee de main rejetee : %s"
                                    % fields.get("detail"), "bad")
        elif kind == "registered":
            self.host_console.write("annonce sur l'antenne %s:%s"
                                    % (fields["relay"], fields["port"]), "good")
        elif kind == "relay_error":
            self.host_console.write("antenne : %s (nouvel essai dans %.0f s)"
                                    % (fields["detail"], fields["retry_in"]),
                                    "warn")
        elif kind == "listening":
            self.host_console.write("en ecoute sur %s:%s"
                                    % (fields["bind"], fields["port"]), "good")
        elif kind == "error":
            self.host_console.write(fields.get("detail", ""), "bad")
        self._refresh_log()

    # -- actions : connexion -------------------------------------------
    def _do_connect(self):
        target = self.target_var.get().strip()
        if not target:
            self.client_console.write("indiquez la machine a joindre", "bad")
            return
        via = None
        port = 7700
        if self.cmode_var.get() == "relay":
            via = self.cvia_var.get().strip()
            if not via:
                self.client_console.write("indiquez l'adresse de l'antenne",
                                          "bad")
                return
        else:
            try:
                port = int(self.cport_var.get())
            except ValueError:
                self.client_console.write("port invalide", "bad")
                return

        self.connect_btn.configure(state="disabled")
        self.client_status.set("connexion...")
        threading.Thread(target=self._connect_worker,
                         args=(target, port, via), daemon=True).start()

    def _gui_verify(self, core, status, sas):
        """Appele depuis le fil reseau : bloque sur la reponse humaine."""
        answer = ModalAnswer(default=False)
        self._ui(verify_dialog, self.root, core, status, sas, answer)
        return answer.wait(300)

    def _gui_password(self):
        answer = ModalAnswer(default=None)
        self._ui(password_dialog, self.root, answer)
        answer.done.wait(300)
        return answer.value or ""

    def _connect_worker(self, target, port, via):
        try:
            session = client.connect(
                target, port=port, via=via, name=socket.gethostname(),
                verify_cb=self._gui_verify, password_cb=self._gui_password,
                status_cb=lambda m: self._ui(self.client_console.write, m),
                quality=self.quality_var.get())
        except client.SessionRejected as exc:
            reasons = {"bad_password": "mot de passe refuse par l'hote",
                       "consent_denied": "l'utilisateur de l'hote a refuse"}
            self._ui(self.client_console.write,
                     "session refusee : %s" % reasons.get(str(exc), exc), "bad")
            self._ui(self._connect_done, "refuse")
            return
        except handshake.HandshakeError as exc:
            self._ui(self.client_console.write, str(exc), "bad")
            self._ui(self._connect_done, "echec")
            return
        except Exception as exc:
            self._ui(self.client_console.write, "echec : %s" % exc, "bad")
            self._ui(self._connect_done, "echec")
            return
        self._ui(self._open_viewer, session)

    def _connect_done(self, state):
        self.connect_btn.configure(state="normal")
        self.client_status.set(state)

    def _open_viewer(self, session):
        self.client_console.write("session ouverte", "good")
        self._connect_done("session ouverte")
        win = tk.Toplevel(self.root)
        win.title("%s - %s" % (APP_NAME, session.core["machine_id"]))
        win.geometry("1100x700")
        win.configure(bg=BG)
        status_var = tk.StringVar()
        frame = tk.Frame(win, bg=BG)
        frame.pack(fill="both", expand=True)
        tk.Label(win, textvariable=status_var, anchor="w", bg=CARD, fg=ACCENT,
                 font=("Consolas", 9)).pack(fill="x")

        def closed():
            self.client_console.write("session fermee", "info")
            self.client_status.set("pret")
            self.viewer_ctrl = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        self.viewer_ctrl = client.ViewerController(frame, session, status_var,
                                                   on_closed=closed)
        win.protocol("WM_DELETE_WINDOW", self.viewer_ctrl.close)

    # -- fin ------------------------------------------------------------
    def _on_quit(self):
        if self.viewer_ctrl:
            self.viewer_ctrl.close()
        if self.service:
            self.service.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    RdlabApp().run()


if __name__ == "__main__":
    main()
