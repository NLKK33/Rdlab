"""Etapes 4-5 : injection des evenements souris et clavier cote hote.

Chemin complet d'un clic :

  1. Client : le toolkit graphique (tkinter) recoit un evenement natif
     du systeme d'exploitation du client. Coordonnees en pixels DANS la
     fenetre d'affichage.
  2. Client : normalisation en coordonnees relatives (0.0 -> 1.0). C'est
     le point cle : l'hote et le client n'ont ni la meme resolution ni le
     meme facteur d'echelle. Transmettre des pixels absolus casserait
     des que la fenetre est redimensionnee.
  3. Reseau : message MOUSE_EVENT, chiffre, ~40 octets.
  4. Hote : denormalisation vers les pixels de l'ecran partage.
  5. Hote : appel a l'API d'injection du systeme.

Ce que "injection" veut dire selon le systeme :
  - Windows : SendInput() -> l'evenement entre dans la file d'entree au
    niveau de la session interactive. (Les fenetres elevees / UAC sont
    protegees par l'UIPI : un processus non eleve ne peut pas les
    piloter. C'est une protection legitime, on ne la contourne pas.)
  - Linux/X11 : XTEST. Wayland : pas d'injection globale par design, il
    faut passer par un portail avec consentement explicite (RemoteDesktop
    portal) - ce qui est exactement l'esprit de ce labo.
  - macOS : CGEvent, sous reserve de l'autorisation "Accessibilite"
    accordee a la main par l'utilisateur dans les Reglages.

pynput fait l'abstraction de tout ca pour nous.

CLAVIER : on transmet des evenements "touche enfoncee" / "touche
relachee", PAS des caracteres. C'est indispensable pour :
  - les raccourcis (Ctrl+C = down Ctrl, down C, up C, up Ctrl) ;
  - les touches maintenues (deplacement dans un jeu, selection Shift) ;
  - les dispositions clavier differentes entre les deux machines.
Si on transmettait "le caractere c", l'hote ne saurait jamais que Ctrl
etait enfonce.
"""

try:
    from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
    from pynput.mouse import Button, Controller as MouseController
    HAVE_INPUT = True
except ImportError:                                   # pragma: no cover
    HAVE_INPUT = False

# Correspondance keysym (Tk, cote client) -> touche pynput (cote hote).
SPECIAL_KEYS = {}
if HAVE_INPUT:
    SPECIAL_KEYS = {
        "Return": Key.enter, "KP_Enter": Key.enter, "BackSpace": Key.backspace,
        "Tab": Key.tab, "Escape": Key.esc, "space": Key.space,
        "Shift_L": Key.shift, "Shift_R": Key.shift_r,
        "Control_L": Key.ctrl, "Control_R": Key.ctrl_r,
        "Alt_L": Key.alt, "Alt_R": Key.alt_gr,
        "Super_L": Key.cmd, "Super_R": Key.cmd_r,
        "Caps_Lock": Key.caps_lock, "Delete": Key.delete,
        "Insert": Key.insert, "Home": Key.home, "End": Key.end,
        "Prior": Key.page_up, "Next": Key.page_down,
        "Left": Key.left, "Right": Key.right, "Up": Key.up, "Down": Key.down,
        "Print": Key.print_screen, "Pause": Key.pause,
        "Menu": Key.menu, "Num_Lock": Key.num_lock,
    }
    for _i in range(1, 13):
        SPECIAL_KEYS["F%d" % _i] = getattr(Key, "f%d" % _i)

BUTTONS = {}
if HAVE_INPUT:
    BUTTONS = {"left": Button.left, "right": Button.right,
               "middle": Button.middle}


class InputInjector:
    """Applique les evenements recus. Respecte les permissions de session.

    `allow_input=False` donne un mode "observation seule" : le client
    voit l'ecran mais ne peut rien declencher. C'est de l'AUTORISATION,
    decidee apres l'authentification, et c'est le bon endroit pour la
    faire respecter : cote hote, jamais cote client (un client modifie
    enverrait les evenements quand meme).
    """

    def __init__(self, width, height, allow_input=True):
        # En observation seule, aucune injection n'aura lieu : on ne doit
        # donc pas exiger la bibliotheque d'injection. C'est aussi une
        # propriete de securite utile -- une machine qui ne veut que du
        # partage d'ecran peut ne jamais installer pynput.
        if allow_input and not HAVE_INPUT:
            raise RuntimeError(
                "controle demande mais pynput est absent : pip install pynput"
                " (ou lancez l hote avec --view-only)")
        self.width = width
        self.height = height
        self.allow_input = allow_input
        self._mouse = MouseController() if allow_input else None
        self._keyboard = KeyboardController() if allow_input else None
        self._pressed = set()      # pour tout relacher en fin de session

    # -- souris ------------------------------------------------------
    def _to_pixels(self, nx, ny):
        x = int(max(0.0, min(1.0, float(nx))) * (self.width - 1))
        y = int(max(0.0, min(1.0, float(ny))) * (self.height - 1))
        return x, y

    def mouse_event(self, ev):
        if not self.allow_input:
            return
        action = ev.get("action")
        if "x" in ev and "y" in ev:
            self._mouse.position = self._to_pixels(ev["x"], ev["y"])
        if action == "move":
            return
        if action == "scroll":
            self._mouse.scroll(int(ev.get("dx", 0)), int(ev.get("dy", 0)))
            return
        button = BUTTONS.get(ev.get("button", "left"))
        if button is None:
            return
        if action == "down":
            self._mouse.press(button)
        elif action == "up":
            self._mouse.release(button)
        elif action == "click":
            self._mouse.click(button, int(ev.get("count", 1)))

    # -- clavier -----------------------------------------------------
    @staticmethod
    def resolve_key(keysym):
        if keysym in SPECIAL_KEYS:
            return SPECIAL_KEYS[keysym]
        if len(keysym) == 1:
            return KeyCode.from_char(keysym)
        return None

    def key_event(self, ev):
        if not self.allow_input:
            return
        key = self.resolve_key(ev.get("keysym", ""))
        if key is None:
            return
        if ev.get("action") == "down":
            self._keyboard.press(key)
            self._pressed.add(ev["keysym"])
        else:
            self._keyboard.release(key)
            self._pressed.discard(ev["keysym"])

    def release_all(self):
        """A appeler IMPERATIVEMENT en fin de session.

        Si la connexion tombe pendant que Ctrl est enfonce, l'hote reste
        avec une touche modificatrice bloquee jusqu'au redemarrage. Tout
        client de bureau a distance serieux gere ce cas."""
        if not self.allow_input:
            return
        for keysym in list(self._pressed):
            key = self.resolve_key(keysym)
            if key is not None:
                try:
                    self._keyboard.release(key)
                except Exception:
                    pass
        self._pressed.clear()
        if self.allow_input:
            for button in BUTTONS.values():
                try:
                    self._mouse.release(button)
                except Exception:
                    pass
