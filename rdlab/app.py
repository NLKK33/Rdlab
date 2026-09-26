"""rdlab - l'application.

    python -m rdlab

Une seule fenetre pour les deux roles : partager son ecran, ou se
connecter a une autre machine.

POURQUOI UNE APPLICATION ET PAS JUSTE DES SCRIPTS

Les decisions de securite de ce logiciel sont prises par un HUMAIN :
accorder une session, verifier une empreinte, activer l'acces sans
surveillance. En ligne de commande, ces decisions se prennent dans un
terminal que personne ne regarde. Ici, elles sont des boites de dialogue
modales, avec le defaut sur "refuser" et un compte a rebours visible.

La regle de non-furtivite est appliquee dans l'interface elle-meme :
quand une session est active, un bandeau rouge nomme qui vous observe et
reste visible. On ne peut pas partager son ecran sans le voir.

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

L'apparence (cartes et boutons arrondis, palette) vit dans theme.py.
Ce module ne definit aucune couleur.
"""

import socket
import threading
import time
import tkinter as tk

from . import auth, client, handshake, host, identity, prefs as prefs_mod
from . import rendezvous
from . import session_log
from . import theme as T

APP_NAME = "rdlab"
CONSENT_TIMEOUT = 60


# =====================================================================
#  Conteneurs
# =====================================================================

class ScrollFrame(tk.Frame):
    """Cadre defilant.

    Un ecran de portable fait 768 px de haut : sans defilement, le bouton
    principal peut se retrouver sous la ligne de flottaison et
    l'application parait cassee."""

    def __init__(self, parent):
        super().__init__(parent, bg=T.BG)
        self._canvas = tk.Canvas(self, bg=T.BG, highlightthickness=0, bd=0)
        self.body = tk.Frame(self._canvas, bg=T.BG)
        self._win = self._canvas.create_window((0, 0), window=self.body,
                                               anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self._canvas.bind("<Configure>", self._on_canvas)
        self.bar = T.ScrollBar(self, self._canvas)
        self._canvas.configure(yscrollcommand=self.bar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y", padx=(6, 0))
        # La molette n'est PAS liee ici : une liaison posee sur ce cadre est
        # perdue des que le pointeur survole un enfant (carte, libelle,
        # console). C'est l'application qui la capte globalement et la
        # redirige vers la page visible -- voir RdlabApp._on_wheel.

    def _on_body(self, _event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, event):
        self._canvas.itemconfigure(self._win, width=event.width)

    def scroll(self, units):
        self._canvas.yview_scroll(units, "units")

    def to(self, fraction):
        self._canvas.yview_moveto(fraction)


class Collapsible(tk.Frame):
    """Section repliee par defaut.

    Tout ce qui n'est pas necessaire a un usage courant va la-dedans :
    l'ecran principal ne doit montrer que l'identifiant, l'antenne et le
    bouton."""

    def __init__(self, parent, title_text):
        super().__init__(parent, bg=T.BG)
        self._open = False
        self._title = title_text
        self._head = tk.Label(self, text=self._label(), bg=T.BG, fg=T.FG_DIM,
                              font=T.F_BODY_B, anchor="w", cursor="hand2",
                              padx=4, pady=6)
        self._head.pack(fill="x")
        self._head.bind("<Button-1>", lambda e: self.toggle())
        self._head.bind("<Enter>", lambda e: self._head.configure(fg=T.FG))
        self._head.bind("<Leave>", lambda e: self._head.configure(fg=T.FG_DIM))
        self._card = T.Card(self, padding=18)
        self.body = self._card.body

    def _label(self):
        return ("▾  %s" if self._open else "▸  %s") % self._title

    def toggle(self):
        self._open = not self._open
        self._head.configure(text=self._label())
        if self._open:
            self._card.pack(fill="x", pady=(0, 4))
        else:
            self._card.pack_forget()


class ConsoleBox(tk.Text):
    """Journal en lecture seule, colore par gravite."""

    def __init__(self, parent, height=10):
        super().__init__(parent, height=height, bg=T.BG, fg=T.FG_DIM,
                         relief="flat", font=T.F_CODE, wrap="word", padx=12,
                         pady=10, highlightthickness=1,
                         highlightbackground=T.EDGE, borderwidth=0,
                         insertbackground=T.ACCENT)
        for tag, color in (("info", T.MUTED), ("good", T.OK), ("warn", T.WARN),
                           ("bad", T.BAD), ("accent", T.ACCENT)):
            self.tag_config(tag, foreground=color)
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
        self.done = threading.Event()

    def set(self, value):
        self.value = value
        self.done.set()

    def wait(self, timeout):
        self.done.wait(timeout)
        return self.value


def _modal(root, window_title, width=480):
    win = tk.Toplevel(root)
    win.title(window_title)
    win.configure(bg=T.BG)
    win.transient(root)
    win.resizable(False, False)
    win.grab_set()
    root.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - width) // 2
    y = root.winfo_rooty() + 90
    win.geometry("%dx60+%d+%d" % (width, max(0, x), max(0, y)))
    card = T.Card(win, padding=24)
    card.pack(fill="both", expand=True, padx=14, pady=14)
    return win, card.body


def _fit(win, width):
    win.update_idletasks()
    win.geometry("%dx%d" % (width, win.winfo_reqheight()))


def _row(parent, key, value):
    r = tk.Frame(parent, bg=T.SURFACE)
    r.pack(fill="x", pady=3)
    tk.Label(r, text=key, bg=T.SURFACE, fg=T.MUTED, font=T.F_SMALL, width=13,
             anchor="w").pack(side="left")
    tk.Label(r, text=value, bg=T.SURFACE, fg=T.FG, font=T.F_BODY,
             anchor="w").pack(side="left")


def consent_dialog(root, request, answer):
    """Consentement explicite : defaut = refus, expiration = refus."""
    win, b = _modal(root, "Demande de connexion")

    def close(value):
        if not answer.done.is_set():
            answer.set(value)
        try:
            win.grab_release()
            win.destroy()
        except tk.TclError:
            pass

    win.protocol("WM_DELETE_WINDOW", lambda: close(False))

    tk.Label(b, text="Une machine demande a voir votre ecran", bg=T.SURFACE,
             fg=T.FG, font=T.F_TITLE).pack(anchor="w")
    T.hint(b, "Rien ne sera partage tant que vous n'avez pas accepte.").pack(
        anchor="w", pady=(2, 16))

    _row(b, "Client", request["client"])
    _row(b, "Adresse", request["peer"])
    _row(b, "Permissions", "observation seule" if request.get("view_only")
         else "controle clavier/souris")

    T.separator(b).pack(fill="x", pady=16)
    tk.Label(b, text="CODE DE VERIFICATION", bg=T.SURFACE, fg=T.MUTED,
             font=T.F_SMALL).pack()
    tk.Label(b, text=request["sas"], bg=T.SURFACE, fg=T.ACCENT,
             font=T.F_SAS).pack(pady=2)
    tk.Label(b, text="Il doit etre IDENTIQUE a celui affiche chez le client.\n"
                     "S'ils different, quelqu'un s'interpose : refusez.",
             bg=T.SURFACE, fg=T.FG_DIM, font=T.F_SMALL,
             justify="center").pack(pady=(0, 6))

    countdown = tk.StringVar()
    tk.Label(b, textvariable=countdown, bg=T.SURFACE, fg=T.MUTED,
             font=T.F_SMALL).pack(pady=(0, 16))

    row = tk.Frame(b, bg=T.SURFACE)
    row.pack(fill="x")
    T.RoundButton(row, "Refuser", lambda: close(False), "ghost", width=150,
                  page_bg=T.SURFACE).pack(side="left", expand=True, fill="x",
                                          padx=(0, 6))
    T.RoundButton(row, "Autoriser", lambda: close(True), "primary", width=150,
                  page_bg=T.SURFACE).pack(side="left", expand=True, fill="x",
                                          padx=(6, 0))

    deadline = time.monotonic() + CONSENT_TIMEOUT

    def tick():
        left = deadline - time.monotonic()
        if left <= 0:
            close(False)
            return
        countdown.set("Refus automatique dans %d s" % int(left))
        win.after(500, tick)

    tick()
    _fit(win, 480)


def verify_dialog(root, core, status, sas, answer):
    """Verification de l'identite de l'hote, cote client (TOFU)."""
    win, b = _modal(root, "Verifier l'hote")

    def close(value):
        if not answer.done.is_set():
            answer.set(value)
        try:
            win.grab_release()
            win.destroy()
        except tk.TclError:
            pass

    win.protocol("WM_DELETE_WINDOW", lambda: close(False))

    heads = {"new": ("Premiere connexion a cette machine", T.WARN),
             "match": ("Machine deja connue, empreinte inchangee", T.OK),
             "mismatch": ("L'empreinte de cette machine a change", T.BAD)}
    text, color = heads.get(status, heads["new"])
    tk.Label(b, text=text, bg=T.SURFACE, fg=color, font=T.F_TITLE).pack(
        anchor="w")

    if status == "mismatch":
        tk.Label(b, text="Cet identifiant etait associe a une AUTRE cle.\n"
                         "Reinstallation de l'hote... ou interception.\n"
                         "En cas de doute, annulez.",
                 bg=T.SURFACE, fg=T.BAD, font=T.F_SMALL, justify="left",
                 anchor="w").pack(fill="x", pady=(6, 0))

    T.hint(b, "Comparez ces valeurs avec ce qui s'affiche sur l'autre "
              "machine.").pack(anchor="w", pady=(2, 14))

    T.hint(b, "IDENTIFIANT").pack(anchor="w")
    tk.Label(b, text=core["machine_id"], bg=T.SURFACE, fg=T.FG,
             font=(T.MONO, 13, "bold"), anchor="w").pack(fill="x")
    T.hint(b, "EMPREINTE").pack(anchor="w", pady=(10, 0))
    tk.Label(b, text=core["fingerprint"], bg=T.SURFACE, fg=T.FG_DIM,
             font=(T.MONO, 8), anchor="w", justify="left",
             wraplength=400).pack(fill="x")

    T.separator(b).pack(fill="x", pady=14)
    tk.Label(b, text="CODE DE VERIFICATION", bg=T.SURFACE, fg=T.MUTED,
             font=T.F_SMALL).pack()
    tk.Label(b, text=sas, bg=T.SURFACE, fg=T.ACCENT, font=T.F_SAS).pack(
        pady=(2, 16))

    row = tk.Frame(b, bg=T.SURFACE)
    row.pack(fill="x")
    T.RoundButton(row, "Annuler", lambda: close(False), "ghost", width=150,
                  page_bg=T.SURFACE).pack(side="left", expand=True, fill="x",
                                          padx=(0, 6))
    T.RoundButton(row, "C'est bien l'hote", lambda: close(True), "primary",
                  width=170, page_bg=T.SURFACE).pack(side="left", expand=True,
                                                     fill="x", padx=(6, 0))
    _fit(win, 480)
    return win


def password_dialog(root, answer, window_title="Mot de passe de l'hote",
                    confirm=False):
    win, b = _modal(root, window_title, 440)

    def close(value):
        if not answer.done.is_set():
            answer.set(value)
        try:
            win.grab_release()
            win.destroy()
        except tk.TclError:
            pass

    win.protocol("WM_DELETE_WINDOW", lambda: close(None))

    tk.Label(b, text=window_title, bg=T.SURFACE, fg=T.FG,
             font=T.F_TITLE).pack(anchor="w")
    T.hint(b, "10 caracteres minimum. Il protege l'acces a votre ecran :\n"
              "choisissez-en un different de celui de votre session Windows."
           if confirm else
           "Il ne quittera jamais cette machine : seule une preuve\n"
           "cryptographique est transmise.").pack(anchor="w", pady=(4, 14))

    v1, v2 = tk.StringVar(), tk.StringVar()
    e1 = T.entry(b, v1, show="•")
    e1.pack(fill="x", ipady=7)
    if confirm:
        T.hint(b, "Confirmation").pack(anchor="w", pady=(10, 2))
        T.entry(b, v2, show="•").pack(fill="x", ipady=7)
    err = tk.StringVar()
    tk.Label(b, textvariable=err, bg=T.SURFACE, fg=T.BAD,
             font=T.F_SMALL, anchor="w").pack(fill="x", pady=(6, 0))

    def ok():
        if confirm:
            if len(v1.get()) < 10:
                err.set("10 caracteres minimum")
                return
            if v1.get() != v2.get():
                err.set("les deux saisies different")
                return
        close(v1.get())

    row = tk.Frame(b, bg=T.SURFACE)
    row.pack(fill="x", pady=(14, 0))
    T.RoundButton(row, "Annuler", lambda: close(None), "ghost", width=130,
                  page_bg=T.SURFACE).pack(side="left", expand=True, fill="x",
                                          padx=(0, 6))
    T.RoundButton(row, "Valider", ok, "primary", width=130,
                  page_bg=T.SURFACE).pack(side="left", expand=True, fill="x",
                                          padx=(6, 0))
    e1.bind("<Return>", lambda e: ok())
    e1.focus_set()
    _fit(win, 440)
    return win


# =====================================================================
#  Le pont moteur -> interface
# =====================================================================

class GuiHooks(host.HostHooks):
    """Traduit les evenements du moteur en actions sur l'interface.

    Aucun widget n'est touche ici : tout passe par app._ui(), qui reporte
    l'appel sur la boucle tkinter."""

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
        self.prefs = prefs_mod.Prefs()
        self.service = None
        self.viewer_ctrl = None
        self.session_active = False

        self.root = tk.Tk()
        self.root.title("%s - bureau a distance" % APP_NAME)
        self.root.geometry("880x760")
        self.root.minsize(760, 600)
        self.root.configure(bg=T.BG)
        self._build()
        self._refresh_credentials()
        self._bind_scrolling()
        self.root.after(300, self._check_relay)
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit)

    def _bind_scrolling(self):
        """La molette est captee au niveau de l'application.

        Liee widget par widget, elle cesse de fonctionner des que le
        pointeur survole une carte ou un libelle : Tk retire la liaison
        du parent en entrant dans l'enfant. C'etait le bug qui empechait
        de descendre en plein ecran."""
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        self.root.bind_all("<Button-4>", lambda e: self._scroll_page(-3))
        self.root.bind_all("<Button-5>", lambda e: self._scroll_page(3))
        self.root.bind_all("<Prior>", lambda e: self._scroll_page(-10))
        self.root.bind_all("<Next>", lambda e: self._scroll_page(10))

    def _scroll_page(self, units):
        self._scrolls[self._current].scroll(units)

    def _on_wheel(self, event):
        # Une autre fenetre (la visionneuse) gere sa propre molette :
        # sans ce test, faire defiler l'ecran distant ferait aussi bouger
        # la fenetre principale.
        try:
            if event.widget.winfo_toplevel() is not self.root:
                return
        except Exception:
            return
        # Un champ de texte defile lui-meme s'il deborde.
        if isinstance(event.widget, tk.Text):
            first, last = event.widget.yview()
            if (last - first) < 0.999:
                return
        self._scroll_page(-1 * (event.delta // 120))

    def _ui(self, fn, *args):
        """Reporte un appel sur le fil de l'interface. Seul point de
        passage autorise depuis un fil de fond."""
        try:
            self.root.after(0, lambda: fn(*args))
        except RuntimeError:
            pass

    # -- structure -----------------------------------------------------
    def _build(self):
        header = tk.Frame(self.root, bg=T.BG)
        header.pack(fill="x", padx=26, pady=(20, 4))
        left = tk.Frame(header, bg=T.BG)
        left.pack(side="left")
        T.Logo(left).pack(side="left", padx=(0, 10))
        tk.Label(left, text=APP_NAME, bg=T.BG, fg=T.FG,
                 font=(T.FONT, 17, "bold")).pack(side="left")
        self.state_pill = T.Pill(header)
        self.state_pill.pack(side="right", pady=4)
        self.state_pill.set("Inactif", T.MUTED, T.SURFACE_2)

        nav = tk.Frame(self.root, bg=T.BG)
        nav.pack(fill="x", padx=26, pady=(10, 0))
        self.tabs = T.NavTabs(nav, ["Partager mon ecran", "Se connecter",
                                    "Journal"], self._on_tab)
        self.tabs.pack(side="left")

        self.alert = tk.Frame(self.root, bg=T.BG)
        self.alert_card = T.Card(self.alert, fill=T.BAD, outline=T.BAD,
                                 padding=13)
        self.alert_card.pack(fill="x")
        self.alert_text = tk.Label(self.alert_card.body, text="", bg=T.BAD,
                                   fg="#FFFFFF", font=T.F_BODY_B, anchor="w")
        self.alert_text.pack(fill="x")

        self.pages = tk.Frame(self.root, bg=T.BG)
        self.pages.pack(fill="both", expand=True, padx=26, pady=(14, 20))
        self._scrolls = []
        self._page_frames = []
        for _ in range(3):
            sc = ScrollFrame(self.pages)
            self._scrolls.append(sc)
            self._page_frames.append(sc.body)
        self._build_share(self._page_frames[0])
        self._build_connect(self._page_frames[1])
        self._build_log(self._page_frames[2])
        self._scrolls[0].pack(fill="both", expand=True)
        self._current = 0

    def _on_tab(self, index):
        self._scrolls[self._current].pack_forget()
        self._scrolls[index].pack(fill="both", expand=True)
        self._current = index
        if index == 2:
            self._refresh_log()

    # -- ecran : partager ----------------------------------------------
    def _build_share(self, root):
        T.title(root, "Partager mon ecran").pack(anchor="w", pady=(0, 4))
        tk.Label(root, text="Donnez votre identifiant a la personne qui doit "
                            "se connecter.", bg=T.BG, fg=T.FG_DIM,
                 font=T.F_BODY, anchor="w").pack(fill="x", pady=(0, 16))

        idc = T.Card(root, padding=22)
        idc.pack(fill="x", pady=(0, 12))
        b = idc.body
        T.hint(b, "IDENTIFIANT DE CETTE MACHINE").pack(anchor="w")
        row = tk.Frame(b, bg=T.SURFACE)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text=self.device.machine_id, bg=T.SURFACE, fg=T.FG,
                 font=T.F_ID).pack(side="left")
        T.RoundButton(row, "Copier", self._copy_id, "ghost", height=36,
                      width=94, page_bg=T.SURFACE).pack(side="left", padx=16)

        self.mode_var = tk.StringVar(value=self.prefs.get("share_mode"))
        self.relay_var = tk.StringVar(value=self.prefs.get("relay"))
        self.relay_pill = self._relay_row(root, self.relay_var)

        self.pw_state = tk.StringVar()
        self.pw_label = tk.Label(root, textvariable=self.pw_state, bg=T.BG,
                                 fg=T.FG_DIM, font=T.F_BODY, anchor="w")
        self.pw_label.pack(fill="x", pady=(0, 8))

        self.share_btn = T.RoundButton(root, "Demarrer le partage",
                                       self._toggle_share, "primary",
                                       height=50, radius=13)
        self.share_btn.pack(fill="x")

        self.host_status = tk.StringVar(value="A l'arret")
        tk.Label(root, textvariable=self.host_status, bg=T.BG, fg=T.MUTED,
                 font=T.F_SMALL, anchor="w").pack(fill="x", pady=(10, 4))

        self.endsession_btn = T.RoundButton(root, "Terminer la session",
                                            self._end_session, "danger",
                                            height=40, width=200)
        self._set_endsession(False)

        adv = Collapsible(root, "Options avancees")
        adv.pack(fill="x", pady=(10, 0))
        a = adv.body
        T.RoundButton(a, "Definir le mot de passe d'acces", self._set_password,
                      "ghost", height=38, width=250,
                      page_bg=T.SURFACE).pack(anchor="w", pady=(0, 12))

        T.hint(a, "ADRESSE DE L'ANTENNE").pack(anchor="w")
        self.relay_entry = T.entry(a, self.relay_var)
        self.relay_entry.pack(fill="x", ipady=6, pady=(4, 2))
        self.relay_entry.bind("<FocusOut>", lambda e: self._relay_changed())
        self.relay_entry.bind("<Return>", lambda e: self._relay_changed())
        T.hint(a, "Deja renseignee. Ne la changez que pour utiliser une "
                  "autre antenne.").pack(fill="x", pady=(0, 12))

        self.viewonly_var = tk.BooleanVar(value=self.prefs.get("view_only"))
        T.check(a, "Observation seule (ne pas appliquer clavier et souris)",
                self.viewonly_var,
                lambda: self.prefs.set("view_only",
                                       self.viewonly_var.get())).pack(fill="x")
        self.unattended_var = tk.BooleanVar(value=self.creds.unattended)
        T.check(a, "Accepter sans me demander (acces sans surveillance)",
                self.unattended_var, self._toggle_unattended).pack(
            fill="x", pady=(6, 0))
        T.hint(a, "Desactive par defaut, exige un mot de passe. Toutes les "
                  "sessions restent journalisees.").pack(fill="x", padx=24)

        T.separator(a).pack(fill="x", pady=14)
        T.radio(a, "Passer par l'antenne (recommande)", self.mode_var, "relay",
                self._refresh_mode).pack(fill="x")
        T.radio(a, "Reseau local uniquement (ecouter un port)", self.mode_var,
                "direct", self._refresh_mode).pack(fill="x")
        lan = tk.Frame(a, bg=T.SURFACE)
        lan.pack(fill="x", padx=24, pady=(6, 0))
        self.bind_var = tk.StringVar(value=self._guess_lan_ip())
        self.port_var = tk.StringVar(value=self.prefs.get("lan_port"))
        T.hint(lan, "Adresse").pack(side="left")
        self.bind_entry = T.entry(lan, self.bind_var, width=16)
        self.bind_entry.pack(side="left", padx=8, ipady=4)
        T.hint(lan, "Port").pack(side="left", padx=(8, 0))
        self.port_entry = T.entry(lan, self.port_var, width=7)
        self.port_entry.pack(side="left", padx=8, ipady=4)

        T.separator(a).pack(fill="x", pady=14)
        T.hint(a, "EMPREINTE DE CETTE MACHINE").pack(anchor="w")
        fp = tk.Frame(a, bg=T.SURFACE)
        fp.pack(fill="x", pady=(4, 0))
        tk.Label(fp, text=self.device.fingerprint, bg=T.SURFACE, fg=T.FG_DIM,
                 font=(T.MONO, 8), anchor="w", justify="left",
                 wraplength=440).pack(side="left")
        T.RoundButton(fp, "Copier", self._copy_fingerprint, "ghost",
                      height=32, width=84, page_bg=T.SURFACE).pack(side="left",
                                                                   padx=12)

        det = Collapsible(root, "Details techniques")
        det.pack(fill="x", pady=(4, 0))
        self.host_console = ConsoleBox(det.body, height=9)
        self.host_console.pack(fill="x")
        self._refresh_mode()

    # -- ecran : se connecter ------------------------------------------
    def _build_connect(self, root):
        T.title(root, "Se connecter").pack(anchor="w", pady=(0, 4))
        tk.Label(root, text="Saisissez l'identifiant affiche sur la machine a "
                            "joindre.", bg=T.BG, fg=T.FG_DIM, font=T.F_BODY,
                 anchor="w").pack(fill="x", pady=(0, 16))

        c = T.Card(root, padding=22)
        c.pack(fill="x", pady=(0, 12))
        b = c.body
        T.hint(b, "IDENTIFIANT DE LA MACHINE").pack(anchor="w")
        self.target_var = tk.StringVar(value=self.prefs.get("last_target"))
        self.target_entry = T.entry(b, self.target_var)
        self.target_entry.pack(fill="x", ipady=9, pady=(4, 4))
        self.target_hint = T.hint(
            b, "Les 9 chiffres affiches sur l'autre machine.")
        self.target_hint.pack(anchor="w")

        self.cvia_var = tk.StringVar(value=self.prefs.get("connect_relay")
                                     or self.prefs.get("relay"))
        self.cvia_pill = self._relay_row(root, self.cvia_var)

        self.connect_btn = T.RoundButton(root, "Se connecter",
                                         self._do_connect, "primary",
                                         height=50, radius=13)
        self.connect_btn.pack(fill="x")
        self.client_status = tk.StringVar(value="Pret")
        tk.Label(root, textvariable=self.client_status, bg=T.BG, fg=T.MUTED,
                 font=T.F_SMALL, anchor="w").pack(fill="x", pady=(10, 0))

        adv = Collapsible(root, "Options avancees")
        adv.pack(fill="x", pady=(10, 0))
        a = adv.body
        T.hint(a, "ADRESSE DE L'ANTENNE").pack(anchor="w")
        self.cvia_entry = T.entry(a, self.cvia_var)
        self.cvia_entry.pack(fill="x", ipady=6, pady=(4, 2))
        self.cvia_entry.bind("<FocusOut>", lambda e: self._cvia_changed())
        self.cvia_entry.bind("<Return>", lambda e: self._cvia_changed())
        T.hint(a, "Deja renseignee. Ne la changez que pour utiliser une "
                  "autre antenne.").pack(fill="x", pady=(0, 12))

        self.cmode_var = tk.StringVar(value=self.prefs.get("connect_mode"))
        T.radio(a, "Passer par l'antenne (recommande)", self.cmode_var,
                "relay", self._refresh_cmode).pack(fill="x")
        T.radio(a, "Reseau local (saisir une adresse IP)", self.cmode_var,
                "direct", self._refresh_cmode).pack(fill="x")
        row = tk.Frame(a, bg=T.SURFACE)
        row.pack(fill="x", padx=24, pady=(6, 14))
        T.hint(row, "Port").pack(side="left")
        self.cport_var = tk.StringVar(value=self.prefs.get("connect_port"))
        self.cport_entry = T.entry(row, self.cport_var, width=7)
        self.cport_entry.pack(side="left", padx=8, ipady=4)

        row = tk.Frame(a, bg=T.SURFACE)
        row.pack(fill="x")
        T.hint(row, "Qualite").pack(side="left")
        self.quality_var = tk.StringVar(value=self.prefs.get("quality"))
        for value, text in (("low", "Fluide"), ("balanced", "Equilibre"),
                            ("high", "Nette")):
            T.radio(row, text, self.quality_var, value,
                    lambda: self.prefs.set("quality",
                                           self.quality_var.get())).pack(
                side="left", padx=(12, 0))

        det = Collapsible(root, "Details techniques")
        det.pack(fill="x", pady=(4, 0))
        self.client_console = ConsoleBox(det.body, height=12)
        self.client_console.pack(fill="x")
        self._refresh_cmode()

    # -- ecran : journal -----------------------------------------------
    def _build_log(self, root):
        head = tk.Frame(root, bg=T.BG)
        head.pack(fill="x", pady=(0, 4))
        T.title(head, "Journal").pack(side="left")
        T.RoundButton(head, "Rafraichir", self._refresh_log, "ghost",
                      height=36, width=120).pack(side="right")
        tk.Label(root, text="Toutes les connexions, acceptees comme refusees.",
                 bg=T.BG, fg=T.FG_DIM, font=T.F_BODY, anchor="w").pack(
            fill="x", pady=(0, 16))
        card = T.Card(root, padding=6)
        card.pack(fill="x")
        self.log_box = ConsoleBox(card.body, height=26)
        self.log_box.pack(fill="both", expand=True)

    def _refresh_log(self):
        if not hasattr(self, "log_box"):
            return
        self.log_box.clear()
        entries = session_log.tail(300)
        if not entries:
            self.log_box.write("Aucune connexion enregistree.", "info")
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

    # -- antenne ---------------------------------------------------------
    def _relay_row(self, parent, var):
        """Ligne d'etat de l'antenne : adresse + pastille de joignabilite.

        L'adresse est integree a l'application (config.py) : elle n'a pas
        a occuper un champ de saisie sur l'ecran principal. Ce qui
        interesse l'utilisateur ici, ce n'est pas sa valeur, c'est de
        savoir si elle repond."""
        row = tk.Frame(parent, bg=T.BG)
        row.pack(fill="x", pady=(0, 14))
        tk.Label(row, text="Antenne", bg=T.BG, fg=T.MUTED,
                 font=T.F_SMALL).pack(side="left")
        tk.Label(row, textvariable=var, bg=T.BG, fg=T.FG_DIM,
                 font=T.F_CODE).pack(side="left", padx=8)
        pill = T.Pill(row)
        pill.pack(side="left", padx=6, pady=2)
        pill.set("verification...", T.MUTED, T.SURFACE_2)
        return pill

    def _probe_relay(self):
        """Sonde l'antenne en fond. Aucune trace n'est laissee cote
        serveur : c'est une simple ouverture TCP."""
        address = self.relay_var.get()
        ok, detail = rendezvous.probe(address)
        self._ui(self._show_relay_state, ok, detail)
        if self.cvia_var.get() != address:
            ok2, detail2 = rendezvous.probe(self.cvia_var.get())
            self._ui(self._show_relay_state, ok2, detail2, "client")
        else:
            self._ui(self._show_relay_state, ok, detail, "client")

    def _show_relay_state(self, ok, detail, which="host"):
        pill = self.cvia_pill if which == "client" else self.relay_pill
        if ok:
            pill.set("joignable - %s" % detail, "#FFFFFF", T.OK)
        else:
            pill.set(detail, "#FFFFFF", T.BAD)

    def _check_relay(self):
        for pill in (self.relay_pill, self.cvia_pill):
            pill.set("verification...", T.MUTED, T.SURFACE_2)
        threading.Thread(target=self._probe_relay, daemon=True).start()

    def _relay_changed(self):
        self.prefs.set("relay", self.relay_var.get().strip())
        self._check_relay()

    def _cvia_changed(self):
        self.prefs.set("connect_relay", self.cvia_var.get().strip())
        self._check_relay()

    # -- etat -----------------------------------------------------------
    def _guess_lan_ip(self):
        """Adresse LAN probable. Aucun paquet n'est emis : connect() sur
        UDP ne fait que choisir une route."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))   # reseau de documentation, RFC 5737
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    def _refresh_credentials(self):
        """Au premier lancement, le bouton principal MENE a l'etape
        suivante au lieu d'etre grise.

        Un bouton desactive accompagne d'un avertissement renvoyant vers
        un menu replie donne l'impression d'un logiciel casse. Ici, le
        seul bouton de l'ecran fait toujours la chose a faire
        maintenant."""
        if self.service:
            return
        if self.creds.has_password:
            self.pw_state.set("")
            self.share_btn.set_text("Demarrer le partage")
        else:
            self.pw_state.set("Premiere utilisation : choisissez un mot de "
                              "passe. Il protegera l'acces a votre ecran.")
            self.pw_label.configure(fg=T.FG_DIM)
            self.share_btn.set_text("Choisir un mot de passe")
        self.share_btn.set_kind("primary")
        self.share_btn.set_state("normal")
        self.unattended_var.set(self.creds.unattended)

    def _refresh_mode(self):
        relay = self.mode_var.get() == "relay"
        self.relay_entry.configure(state="normal" if relay else "disabled")
        for w in (self.bind_entry, self.port_entry):
            w.configure(state="disabled" if relay else "normal")
        self.prefs.set("share_mode", self.mode_var.get())

    def _refresh_cmode(self):
        relay = self.cmode_var.get() == "relay"
        self.cvia_entry.configure(state="normal" if relay else "disabled")
        self.cport_entry.configure(state="disabled" if relay else "normal")
        self.target_hint.configure(
            text="Les 9 chiffres affiches sur l'autre machine." if relay
            else "L'adresse IP de l'autre machine sur votre reseau local.")
        self.prefs.set("connect_mode", self.cmode_var.get())

    def _set_endsession(self, active):
        """Le coupe-circuit n'existe que pendant une session : un bouton
        rouge permanent laisserait croire qu'une session est en cours."""
        if active:
            self.endsession_btn.pack(anchor="w", pady=(6, 0))
        else:
            self.endsession_btn.pack_forget()

    def _set_alert(self, text=None):
        """Bandeau rouge de non-furtivite. Absent = rien n'est partage."""
        if text:
            self.alert_text.configure(text=text)
            # `before` plutot qu'un index de widget : l'ordre d'empilement
            # reste correct meme si la structure de la fenetre evolue.
            self.alert.pack(fill="x", padx=26, pady=(12, 0),
                            before=self.pages)
        else:
            self.alert.pack_forget()

    # -- actions : securite ---------------------------------------------
    def _copy_id(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.device.machine_id)
        self.host_console.write("Identifiant copie.", "accent")

    def _copy_fingerprint(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.device.fingerprint)
        self.host_console.write("Empreinte copiee.", "accent")

    def _set_password(self):
        answer = ModalAnswer(default=None)
        win = password_dialog(self.root, answer,
                              "Definir le mot de passe d'acces", confirm=True)
        self.root.wait_window(win)      # on est sur le fil interface
        pw = answer.value
        if not pw:
            return
        try:
            self.creds.set_password(pw)
        except ValueError as exc:
            self.host_console.write(str(exc), "bad")
            return
        session_log.log("password_changed")
        self.host_console.write("Mot de passe enregistre (scrypt N=32768).",
                                "good")
        self._refresh_credentials()
        self.host_status.set("Mot de passe enregistre. Vous pouvez demarrer "
                             "le partage.")

    def _toggle_unattended(self):
        try:
            if self.unattended_var.get():
                self.creds.enable_unattended()
                self.host_console.write(
                    "Acces sans surveillance ACTIVE : les connexions "
                    "authentifiees seront acceptees sans vous demander.",
                    "warn")
            else:
                self.creds.disable_unattended()
                self.host_console.write("Acces sans surveillance desactive.",
                                        "good")
            session_log.log("unattended_changed",
                            value="on" if self.unattended_var.get() else "off")
        except ValueError as exc:
            self.host_console.write(str(exc), "bad")
        self._refresh_credentials()

    # -- actions : partage -----------------------------------------------
    def _toggle_share(self):
        if self.service:
            self._stop_share()
            return
        if not self.creds.has_password:
            self._set_password()      # le bouton mene a l'etape manquante
            return
        self._start_share()

    def _start_share(self):
        if not self.creds.has_password:
            self.host_console.write(
                "Definissez d'abord un mot de passe d'acces.", "bad")
            return
        relay = None
        bind, port = "127.0.0.1", 7700
        if self.mode_var.get() == "relay":
            relay = self.relay_var.get().strip()
            if not relay:
                self.host_console.write("Indiquez l'adresse de l'antenne.",
                                        "bad")
                return
        else:
            bind = self.bind_var.get().strip() or "127.0.0.1"
            try:
                port = int(self.port_var.get())
            except ValueError:
                self.host_console.write("Port invalide.", "bad")
                return

        self.prefs.set("relay", relay or self.prefs.get("relay"))
        self.prefs.set("lan_port", self.port_var.get())
        options = host.HostOptions(bind=bind, port=port, relay=relay,
                                   view_only=self.viewonly_var.get())
        self.service = host.HostService(self.device, self.creds, options,
                                        GuiHooks(self))
        threading.Thread(target=self._run_service, daemon=True).start()
        self.share_btn.set_text("Arreter le partage")
        self.share_btn.set_kind("danger")
        self.state_pill.set("Partage actif", "#FFFFFF", T.ACCENT)
        self._set_alert("PARTAGE ACTIF - cet ordinateur peut etre observe")
        self.host_console.write(
            "Partage demarre (%s)." % ("antenne %s" % relay if relay
                                       else "%s:%d" % (bind, port)), "good")

    def _run_service(self):
        try:
            self.service.start()
        except Exception as exc:
            self._ui(self.host_console.write, "Moteur arrete : %s" % exc,
                     "bad")
        finally:
            self._ui(self._on_service_stopped)

    def _stop_share(self):
        if self.service:
            self.service.stop()
        self.host_console.write("Arret demande.", "info")

    def _on_service_stopped(self):
        self.service = None
        self.session_active = False
        self.share_btn.set_text("Demarrer le partage")
        self.share_btn.set_kind("primary")
        self.state_pill.set("Inactif", T.MUTED, T.SURFACE_2)
        self._set_endsession(False)
        self.host_status.set("A l'arret")
        self._set_alert(None)

    def _end_session(self):
        if self.service:
            self.service.stop_session()

    def _on_host_event(self, kind, fields):
        if kind == "session_stats":
            q = fields["quality"]
            self.host_status.set(
                "Session active - %s img/s cible, qualite %s, %s ms de "
                "latence, %d Ko envoyes"
                % (q["fps"], q["quality"], q["rtt_ms"], fields["kbytes"]))
            return
        if kind == "session_start":
            self.session_active = True
            self._set_endsession(True)
            mode = ("observation seule" if not fields["allow_input"]
                    else "controle clavier et souris")
            self.state_pill.set("Session en cours", "#FFFFFF", T.BAD)
            self._set_alert("SESSION EN COURS - %s vous observe (%s)"
                            % (fields["client"], mode))
            self.host_console.write("Session ouverte avec %s (%s)."
                                    % (fields["client"], mode), "good")
        elif kind == "session_end":
            self.session_active = False
            self._set_endsession(False)
            self.state_pill.set("Partage actif", "#FFFFFF", T.ACCENT)
            self._set_alert("PARTAGE ACTIF - cet ordinateur peut etre observe")
            self.host_console.write("Session terminee (%s s)."
                                    % fields["duration_s"], "info")
        elif kind == "auth_failed":
            self.host_console.write("Mot de passe REFUSE pour %s."
                                    % fields.get("peer"), "bad")
        elif kind == "consent_denied":
            self.host_console.write("Connexion refusee par vous.", "warn")
        elif kind == "handshake_failed":
            self.host_console.write("Poignee de main rejetee : %s"
                                    % fields.get("detail"), "bad")
        elif kind == "registered":
            self.host_console.write("Annonce sur l'antenne %s:%s."
                                    % (fields["relay"], fields["port"]),
                                    "good")
        elif kind == "relay_error":
            self.host_console.write("Antenne : %s (nouvel essai dans %.0f s)."
                                    % (fields["detail"], fields["retry_in"]),
                                    "warn")
        elif kind == "listening":
            self.host_console.write("En ecoute sur %s:%s."
                                    % (fields["bind"], fields["port"]), "good")
        elif kind == "error":
            self.host_console.write(fields.get("detail", ""), "bad")
        self._refresh_log()

    # -- actions : connexion ----------------------------------------------
    def _do_connect(self):
        target = self.target_var.get().strip()
        if not target:
            self.client_console.write("Indiquez la machine a joindre.", "bad")
            return
        via, port = None, 7700
        if self.cmode_var.get() == "relay":
            via = self.cvia_var.get().strip()
            if not via:
                self.client_console.write("Indiquez l'adresse de l'antenne.",
                                          "bad")
                return
        else:
            try:
                port = int(self.cport_var.get())
            except ValueError:
                self.client_console.write("Port invalide.", "bad")
                return

        self.prefs.set("last_target", target)
        self.prefs.set("connect_relay", via or self.prefs.get("connect_relay"))
        self.prefs.set("connect_port", self.cport_var.get())
        self.connect_btn.set_state("disabled")
        self.client_status.set("Connexion en cours...")
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
                     "Session refusee : %s" % reasons.get(str(exc), exc),
                     "bad")
            self._ui(self._connect_done, "Session refusee")
            return
        except handshake.HandshakeError as exc:
            self._ui(self.client_console.write, str(exc), "bad")
            self._ui(self._connect_done, "Echec")
            return
        except Exception as exc:
            self._ui(self.client_console.write, "Echec : %s" % exc, "bad")
            self._ui(self._connect_done, "Echec")
            return
        self._ui(self._open_viewer, session)

    def _connect_done(self, state):
        self.connect_btn.set_state("normal")
        self.client_status.set(state)

    def _open_viewer(self, session):
        self.client_console.write("Session ouverte.", "good")
        self._connect_done("Session ouverte")
        win = tk.Toplevel(self.root)
        win.title("%s - %s" % (APP_NAME, session.core["machine_id"]))
        win.geometry("1100x700")
        win.configure(bg=T.BG)
        status_var = tk.StringVar()
        frame = tk.Frame(win, bg=T.BG)
        frame.pack(fill="both", expand=True)
        tk.Label(win, textvariable=status_var, anchor="w", bg=T.SURFACE,
                 fg=T.FG_DIM, font=T.F_CODE, pady=5).pack(fill="x")

        def closed():
            self.client_console.write("Session fermee.", "info")
            self.client_status.set("Pret")
            self.viewer_ctrl = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        self.viewer_ctrl = client.ViewerController(frame, session, status_var,
                                                   on_closed=closed)
        win.protocol("WM_DELETE_WINDOW", self.viewer_ctrl.close)

    # -- fin ---------------------------------------------------------------
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
