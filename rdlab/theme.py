"""Le systeme visuel de l'application.

Tkinter ne connait ni border-radius, ni ombre, ni degrade. Les cartes et
les boutons arrondis sont donc DESSINES sur un canvas : un polygone lisse
(`smooth=True`) passant par les coins donne un arrondi convaincant pour
un cout nul, la ou une image 9-slice imposerait des ressources externes
et casserait le redimensionnement.

Consequence a garder en tete : un widget arrondi est un Canvas, pas un
Frame. Il se redessine sur <Configure>, et son contenu vit dans un
`create_window`. C'est le prix de l'apparence.

La palette est volontairement resserree : un seul accent, trois niveaux
de surface, trois niveaux de texte. Les couleurs d'etat (succes, alerte,
danger) ne servent qu'aux etats, jamais a la decoration -- sans quoi
l'alerte rouge d'une session en cours se noierait dans le decor.
"""

import tkinter as tk

# --- surfaces, du plus profond au plus proche
BG = "#0F1018"          # fond de page
SURFACE = "#1A1B26"     # carte
SURFACE_2 = "#222432"   # champ, element dans une carte
SURFACE_3 = "#2B2D3D"   # survol
EDGE = "#2A2C3B"        # bordure de carte

# --- texte
FG = "#EDEEF5"          # texte principal
FG_DIM = "#A6A8BD"      # texte secondaire
MUTED = "#6F718A"       # legende, aide

# --- accent unique
ACCENT = "#5B4DF5"
ACCENT_HOVER = "#6E62FF"
ACCENT_DIM = "#3A3270"

# --- etats (jamais decoratifs)
OK = "#3DD68C"
WARN = "#F5A623"
BAD = "#F2545B"

FONT = "Segoe UI"
MONO = "Consolas"

F_DISPLAY = (FONT, 21, "bold")
F_TITLE = (FONT, 13, "bold")
F_BODY = (FONT, 10)
F_BODY_B = (FONT, 10, "bold")
F_SMALL = (FONT, 9)
F_BTN = (FONT, 10, "bold")
F_ID = (MONO, 29, "bold")
F_SAS = (MONO, 30, "bold")
F_CODE = (MONO, 9)


def round_points(x1, y1, x2, y2, r):
    """Points d'un rectangle arrondi, a tracer avec smooth=True.

    Chaque coin est decrit par trois points rapproches : le lissage de Tk
    les transforme en courbe. Le rayon est borne a la moitie du plus petit
    cote, sinon les coins se croisent sur un widget etroit."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def draw_round_rect(canvas, x1, y1, x2, y2, r, **kw):
    return canvas.create_polygon(round_points(x1, y1, x2, y2, r),
                                 smooth=True, **kw)


class Card(tk.Frame):
    """Surface arrondie qui s'adapte a la hauteur de son contenu.

    Le contenu va dans `.body`, un Frame empaquete normalement : c'est la
    propagation de taille habituelle de Tk qui donne sa hauteur a la
    carte. Le canvas qui porte l'arrondi est place DERRIERE en `place`,
    ce qui ne participe pas au calcul de geometrie.

    La premiere version placait au contraire le contenu DANS le canvas via
    create_window. Elle ne marchait pas : Tk ne realise la mise en page
    d'un element de canvas que lorsqu'il devient visible, or le canvas
    partait a 10 px de haut et le contenu etait pose a 20 px. Le contenu
    n'etait donc jamais mappe, ne signalait jamais sa taille, et le canvas
    ne grandissait jamais -- une attente circulaire qui produisait des
    cartes hautes de quelques pixels.

    Le rayon doit rester inferieur au padding, sinon le contenu
    rectangulaire recouvrirait les coins arrondis."""

    def __init__(self, parent, radius=14, fill=SURFACE, outline=EDGE,
                 padding=18, page_bg=BG):
        super().__init__(parent, bg=page_bg)
        self._r = min(radius, padding)
        self._fill, self._outline = fill, outline
        self.canvas = tk.Canvas(self, bg=page_bg, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=fill)
        self.body.pack(fill="both", expand=True, padx=padding, pady=padding)
        self.bind("<Configure>", lambda e: self._redraw())

    def _redraw(self):
        self.canvas.delete("shape")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        draw_round_rect(self.canvas, 1, 1, w - 2, h - 2, self._r,
                        fill=self._fill, outline=self._outline, width=1,
                        tags="shape")


class RoundButton(tk.Canvas):
    """Bouton dessine : arrondi, survol, etat desactive.

    `kind` fixe le role, pas juste la couleur : `primary` est l'action
    unique de l'ecran, `ghost` est secondaire et n'a qu'un contour,
    `danger` interrompt. Un ecran qui contient deux boutons `primary` est
    un ecran mal concu."""

    STYLES = {
        "primary": (ACCENT, ACCENT_HOVER, "#FFFFFF", None),
        "ghost": (None, SURFACE_3, FG, EDGE),
        "danger": (BAD, "#FF6B72", "#FFFFFF", None),
        "success": (OK, "#55E39C", "#08160E", None),
    }

    def __init__(self, parent, text, command, kind="primary", height=42,
                 radius=11, width=None, page_bg=BG, font=None):
        super().__init__(parent, bg=page_bg, highlightthickness=0, bd=0,
                         height=height, width=width or 10)
        self._fill, self._hover, self._fg, self._outline = self.STYLES[kind]
        self._page_bg = page_bg
        self._text = text
        self._command = command
        self._radius = radius
        self._font = font or F_BTN
        self._state = "normal"
        self._over = False
        self._fixed_width = width
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    # -- etat --------------------------------------------------------
    def set_text(self, text):
        self._text = text
        self._redraw()

    def set_kind(self, kind):
        self._fill, self._hover, self._fg, self._outline = self.STYLES[kind]
        self._redraw()

    def set_state(self, state):
        self._state = state
        self.configure(cursor="" if state == "disabled" else "hand2")
        self._redraw()

    # -- interactions -------------------------------------------------
    def _on_enter(self, _e):
        self._over = True
        self._redraw()

    def _on_leave(self, _e):
        self._over = False
        self._redraw()

    def _on_click(self, _e):
        if self._state != "disabled" and self._command:
            self._command()

    # -- rendu ---------------------------------------------------------
    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        disabled = self._state == "disabled"
        fill = self._fill
        if disabled:
            fill = SURFACE_2 if fill else None
        elif self._over and self._hover:
            fill = self._hover
        fg = MUTED if disabled else self._fg
        outline = self._outline or fill or self._page_bg
        draw_round_rect(self, 1, 1, w - 2, h - 2, self._radius,
                        fill=fill or self._page_bg, outline=outline, width=1)
        self.create_text(w / 2, h / 2, text=self._text, fill=fg,
                         font=self._font)


class Pill(tk.Canvas):
    """Etiquette arrondie : sert aux etats, pas a la decoration."""

    def __init__(self, parent, text="", fg=MUTED, fill=SURFACE_2,
                 page_bg=BG, height=26, font=None):
        super().__init__(parent, bg=page_bg, highlightthickness=0, bd=0,
                         height=height)
        self._text, self._fg, self._fill = text, fg, fill
        self._font = font or F_SMALL
        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, text, fg=None, fill=None):
        self._text = text
        if fg:
            self._fg = fg
        if fill:
            self._fill = fill
        self._redraw()
        self.configure(width=max(60, len(self._text) * 7 + 26))

    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        draw_round_rect(self, 1, 1, w - 2, h - 2, h / 2, fill=self._fill,
                        outline=self._fill)
        self.create_text(w / 2, h / 2, text=self._text, fill=self._fg,
                         font=self._font)


class NavTabs(tk.Frame):
    """Navigation en segments, a la place d'un ttk.Notebook.

    Un Notebook impose son rendu natif, qui jure avec des cartes
    dessinees. Ici chaque onglet est un RoundButton dont le role change
    selon la selection."""

    def __init__(self, parent, labels, on_change, page_bg=BG):
        super().__init__(parent, bg=page_bg)
        self._on_change = on_change
        self._buttons = []
        self.current = 0
        for i, text in enumerate(labels):
            b = RoundButton(self, text, lambda i=i: self.select(i),
                            kind="ghost", height=36, radius=9,
                            width=max(120, len(text) * 9 + 30),
                            page_bg=page_bg, font=F_BODY_B)
            b.pack(side="left", padx=(0, 8))
            self._buttons.append(b)
        self.after(50, lambda: self.select(0, notify=False))

    def select(self, index, notify=True):
        self.current = index
        for i, b in enumerate(self._buttons):
            b.set_kind("primary" if i == index else "ghost")
        if notify:
            self._on_change(index)


def entry(parent, textvariable, show=None, width=None):
    """Champ de saisie : pas d'arrondi possible sur un tk.Entry, on mise
    sur un fond distinct et une bordure fine."""
    return tk.Entry(parent, textvariable=textvariable, show=show,
                    bg=SURFACE_2, fg=FG, insertbackground=ACCENT,
                    relief="flat", font=F_BODY, width=width or 10,
                    highlightthickness=1, highlightbackground=EDGE,
                    highlightcolor=ACCENT, disabledbackground=SURFACE,
                    disabledforeground=MUTED)


def title(parent, text, bg=BG):
    return tk.Label(parent, text=text, bg=bg, fg=FG, font=F_DISPLAY,
                    anchor="w")


def section(parent, text, bg=SURFACE):
    return tk.Label(parent, text=text, bg=bg, fg=FG, font=F_TITLE, anchor="w")


def body(parent, text, bg=SURFACE, fg=FG_DIM, font=None):
    return tk.Label(parent, text=text, bg=bg, fg=fg, font=font or F_BODY,
                    anchor="w", justify="left")


def hint(parent, text, bg=SURFACE):
    return tk.Label(parent, text=text, bg=bg, fg=MUTED, font=F_SMALL,
                    anchor="w", justify="left")


def check(parent, text, var, command, bg=SURFACE):
    return tk.Checkbutton(parent, text=text, variable=var, command=command,
                          bg=bg, fg=FG, selectcolor=SURFACE_2,
                          activebackground=bg, activeforeground=FG,
                          font=F_BODY, anchor="w", highlightthickness=0,
                          borderwidth=0, cursor="hand2")


def radio(parent, text, var, value, command, bg=SURFACE):
    return tk.Radiobutton(parent, text=text, variable=var, value=value,
                          command=command, bg=bg, fg=FG,
                          selectcolor=SURFACE_2, activebackground=bg,
                          activeforeground=FG, font=F_BODY, anchor="w",
                          highlightthickness=0, borderwidth=0, cursor="hand2")


def separator(parent, bg=SURFACE):
    return tk.Frame(parent, bg=EDGE, height=1)


class Logo(tk.Canvas):
    """Marque dessinee : un ecran, et un second ecran en surimpression.

    Evite d'embarquer une image -- un fichier de plus a inclure dans
    l'executable, pour 40 pixels."""

    def __init__(self, parent, page_bg=BG, size=30):
        super().__init__(parent, bg=page_bg, highlightthickness=0, bd=0,
                         width=size + 4, height=size + 4)
        s = size
        draw_round_rect(self, 1, 3, s * 0.72, s * 0.72, 5, fill=ACCENT,
                        outline=ACCENT)
        draw_round_rect(self, s * 0.34, s * 0.3, s, s - 1, 5, fill=SURFACE,
                        outline=ACCENT, width=2)
