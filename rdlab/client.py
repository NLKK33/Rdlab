"""Le CLIENT : la machine qui observe et pilote.

Comme rdlab.host, ce module separe le MOTEUR de la PRESENTATION :

    connect(...)          -> ouvre le tuyau, verifie l'identite, authentifie
                             (pur reseau, aucune dependance graphique)
    ViewerController(...)  -> attache une fenetre a une session ouverte

Cette separation permet a rdlab.app d'afficher des boites de dialogue
pour la verification d'empreinte et le mot de passe, la ou la version
console pose les questions dans le terminal -- avec le meme moteur.

Architecture interne de l'affichage :

    [fil reseau]  --queue--> [boucle tkinter]   (affichage)
    [boucle tkinter] -------------------------> [fil envoi] (entrees)

Regle absolue avec tkinter (comme avec tout toolkit graphique) : un seul
fil touche a l'interface. Le fil reseau ne fait que deposer des donnees
dans une file ; la boucle graphique la vide via after().

Deux choix de conception a retenir :

1. COORDONNEES NORMALISEES. La fenetre du client n'a pas la taille de
   l'ecran distant, et peut etre redimensionnee a tout moment. On envoie
   donc des flottants 0.0-1.0 ; l'hote les retraduit en pixels. Le
   protocole devient independant des resolutions.

2. LIMITATION DU DEBIT DE LA SOURIS (throttling). Un toolkit peut
   produire plusieurs centaines d'evenements de deplacement par seconde.
   Les envoyer tous sature le lien pour rien : l'oeil ne distingue pas
   plus de ~60 positions/s. On jette les intermediaires. En revanche on
   n'en jette JAMAIS un clic ou une touche : perdre un "bouton relache"
   laisse l'hote croire que le bouton est toujours enfonce.
"""

import argparse
import getpass
import queue
import socket
import sys
import threading
import time
from io import BytesIO

from . import capture, handshake, identity, protocol, rendezvous

try:
    import tkinter as tk
    from PIL import Image, ImageTk
    HAVE_UI = True
except ImportError:                                   # pragma: no cover
    HAVE_UI = False

MOTION_INTERVAL = 1.0 / 60.0
DEFAULT_PORT = 7700


# =====================================================================
#  Moteur : etablir une session
# =====================================================================

class ClientSession:
    def __init__(self, channel, core, sas, permissions, screen_info):
        self.channel = channel
        self.core = core
        self.sas = sas
        self.permissions = permissions
        self.screen_info = screen_info

    @property
    def remote_size(self):
        return (self.permissions.get("width", 1),
                self.permissions.get("height", 1))


class SessionRejected(Exception):
    pass


def console_verify(core, status, sas):
    """La decision humaine de faire confiance a cet hote.

    'new'      : premiere rencontre -> comparer l'empreinte et le SAS
                 avec ce qui s'affiche sur l'ecran de l'hote.
    'match'    : deja connu, meme cle -> on continue.
    'mismatch' : deja connu, cle DIFFERENTE -> soit l'hote a ete
                 reinstalle, soit quelqu'un s'interpose. On refuse par
                 defaut et on exige une action explicite.
    """
    print("\nHote annonce :")
    print("  identifiant : %s" % core["machine_id"])
    print("  empreinte   : %s" % core["fingerprint"])
    print("  code SAS    : %s  <- doit etre identique des deux cotes" % sas)
    if status == "match":
        print("  -> hote deja connu, empreinte inchangee.")
        return True
    if status == "mismatch":
        print("\n  *** ALERTE : cet identifiant etait associe a une AUTRE")
        print("  *** empreinte. Reinstallation... ou interception.")
        return input("  Accepter quand meme ? tapez ACCEPTER : ") == "ACCEPTER"
    print("  -> premiere connexion a cet hote (TOFU).")
    return input("  Empreinte et SAS verifies ? [o/N] ").strip().lower() in (
        "o", "oui", "y", "yes")


def open_socket(target, port=DEFAULT_PORT, via=None, status_cb=None):
    """Deux facons d'obtenir un tuyau, UN SEUL protocole au-dessus.

    C'est tout l'interet de la separation transport / application : le
    relais change la maniere dont les octets voyagent, pas ce qu'ils
    contiennent. client_handshake() ne sait pas lequel des deux a servi."""
    say = status_cb or (lambda m: None)
    if via:
        relay_host, relay_port = via, rendezvous.DEFAULT_PORT
        if ":" in via:
            relay_host, _, p = via.rpartition(":")
            relay_port = int(p)
        say("demande de %s a l antenne %s:%d..." % (target, relay_host,
                                                    relay_port))
        sock = rendezvous.relay_connect(relay_host, relay_port, target)
        say("apparie par l antenne (elle ne voit que du chiffre)")
        return sock
    say("connexion directe a %s:%d..." % (target, port))
    sock = socket.create_connection((target, port), timeout=15)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(None)
    return sock


def connect(target, port=DEFAULT_PORT, via=None, name=None, verify_cb=None,
            password_cb=None, status_cb=None, quality=None):
    """Ouvre, verifie, authentifie. Retourne une ClientSession prete.

    Ne touche a aucun widget : utilisable depuis un fil de fond."""
    say = status_cb or (lambda m: None)
    name = name or socket.gethostname()
    verify_cb = verify_cb or console_verify
    password_cb = password_cb or (
        lambda: getpass.getpass("Mot de passe de l hote : "))

    sock = open_socket(target, port, via, say)
    channel, core, sas, binding = handshake.client_handshake(
        sock, name, verify_cb)
    say("canal chiffre etabli")
    handshake.client_authenticate(channel, binding, password_cb)
    say("preuve de mot de passe envoyee")

    permissions = None
    screen_info = {}
    while permissions is None:
        msg_type, payload = channel.recv()
        if msg_type == protocol.CONSENT_PENDING:
            say("en attente du consentement de l utilisateur hote...")
        elif msg_type == protocol.SCREEN_INFO:
            screen_info = protocol.as_json(payload)
        elif msg_type == protocol.SESSION_ACCEPTED:
            permissions = protocol.as_json(payload)
        elif msg_type == protocol.SESSION_REJECTED:
            reason = protocol.as_json(payload).get("reason")
            channel.close()
            raise SessionRejected(reason)
    say("session acceptee")
    session = ClientSession(channel, core, sas, permissions, screen_info)
    if quality:
        channel.send(protocol.QUALITY_HINT, {"level": quality})
    return session


# =====================================================================
#  Presentation : la fenetre de visualisation
# =====================================================================

class ViewerWindow:
    def __init__(self, container, session, send_queue, status_var=None):
        self.container = container
        self.session = session
        self.remote_w, self.remote_h = session.remote_size
        self.allow_input = session.permissions.get("allow_input", False)
        self.send_queue = send_queue
        self.incoming = queue.Queue()
        self.canvas = tk.Canvas(container, bg="#0d0d12", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.status_var = status_var or tk.StringVar()
        self._surface = Image.new("RGB", (self.remote_w, self.remote_h),
                                  (13, 13, 18))
        self._photo = None
        self._rect = (0, 0, 1, 1)       # zone d'affichage reelle de l'image
        self._last_motion = 0.0
        self._dirty = True
        self._alive = True
        self.frames = 0
        self.bytes_in = 0
        self._bind_events()
        self.container.after(30, self._pump)

    def destroy(self):
        self._alive = False

    # -- reception ---------------------------------------------------
    def feed(self, payload):
        self.incoming.put(payload)

    def _pump(self):
        """Vide la file reseau, recompose l'image, repeint."""
        if not self._alive:
            return
        drained = 0
        while drained < 8:
            try:
                payload = self.incoming.get_nowait()
            except queue.Empty:
                break
            drained += 1
            self.bytes_in += len(payload)
            try:
                _seq, _q, _kf, tiles = capture.parse_frame(payload)
                for x, y, w, h, data in tiles:
                    self._surface.paste(Image.open(BytesIO(data)), (x, y))
            except Exception:
                continue          # une image corrompue ne doit pas tout casser
            self.frames += 1
            self._dirty = True
        if self._dirty:
            self._repaint()
            self._dirty = False
        self.container.after(25, self._pump)

    def _repaint(self):
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        scale = min(cw / self.remote_w, ch / self.remote_h)
        w = max(1, int(self.remote_w * scale))
        h = max(1, int(self.remote_h * scale))
        ox, oy = (cw - w) // 2, (ch - h) // 2
        self._rect = (ox, oy, w, h)
        img = self._surface.resize((w, h), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("frame")
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo,
                                 tags="frame")
        self.status_var.set(
            "  %s   %d x %d   images %d   %d Ko recus   Echap = quitter"
            % ("CONTROLE" if self.allow_input else "OBSERVATION SEULE",
               self.remote_w, self.remote_h, self.frames,
               self.bytes_in // 1024))

    # -- entrees -----------------------------------------------------
    def _normalize(self, event):
        ox, oy, w, h = self._rect
        nx = (event.x - ox) / float(w)
        ny = (event.y - oy) / float(h)
        return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))

    def _bind_events(self):
        c = self.canvas
        c.bind("<Motion>", self._on_motion)
        for num, btn in ((1, "left"), (2, "middle"), (3, "right")):
            c.bind("<ButtonPress-%d>" % num,
                   lambda e, b=btn: self._on_button(e, b, "down"))
            c.bind("<ButtonRelease-%d>" % num,
                   lambda e, b=btn: self._on_button(e, b, "up"))
        c.bind("<MouseWheel>", self._on_wheel)                 # Windows/macOS
        c.bind("<Button-4>", lambda e: self._on_wheel(e, 1))   # X11
        c.bind("<Button-5>", lambda e: self._on_wheel(e, -1))
        c.bind("<KeyPress>", self._on_key_down)
        c.bind("<KeyRelease>", self._on_key_up)
        c.focus_set()

    def _send(self, msg_type, payload):
        self.send_queue.put((msg_type, payload))

    def _on_motion(self, event):
        now = time.monotonic()
        if now - self._last_motion < MOTION_INTERVAL:
            return                   # on jette : un deplacement peut se perdre
        self._last_motion = now
        nx, ny = self._normalize(event)
        self._send(protocol.MOUSE_EVENT, {"action": "move", "x": nx, "y": ny})

    def _on_button(self, event, button, action):
        self.canvas.focus_set()
        nx, ny = self._normalize(event)
        # jamais throttle : un 'up' perdu bloque le bouton cote hote
        self._send(protocol.MOUSE_EVENT,
                   {"action": action, "button": button, "x": nx, "y": ny})

    def _on_wheel(self, event, direction=None):
        if direction is None:
            direction = 1 if event.delta > 0 else -1
        nx, ny = self._normalize(event)
        self._send(protocol.MOUSE_EVENT,
                   {"action": "scroll", "dx": 0, "dy": direction,
                    "x": nx, "y": ny})

    def _on_key_down(self, event):
        if event.keysym == "Escape":
            self.container.event_generate("<<RdlabClose>>")
            return
        self._send(protocol.KEY_EVENT,
                   {"action": "down", "keysym": event.keysym})

    def _on_key_up(self, event):
        self._send(protocol.KEY_EVENT,
                   {"action": "up", "keysym": event.keysym})


class ViewerController:
    """Relie une ClientSession a une fenetre : fils reseau + widgets."""

    def __init__(self, window, session, status_var=None, on_closed=None):
        self.window = window
        self.session = session
        self.on_closed = on_closed
        self._closed_notified = threading.Event()
        self.send_queue = queue.Queue()
        self.stop = threading.Event()
        self.viewer = ViewerWindow(window, session, self.send_queue, status_var)
        window.bind("<<RdlabClose>>", lambda e: self.close())
        self._threads = [
            threading.Thread(target=self._recv_loop, daemon=True),
            threading.Thread(target=self._send_loop, daemon=True)]
        for t in self._threads:
            t.start()

    def _recv_loop(self):
        try:
            while not self.stop.is_set():
                msg_type, payload = self.session.channel.recv()
                if msg_type == protocol.SCREEN_FRAME:
                    self.viewer.feed(payload)
                elif msg_type == protocol.PING:
                    self.send_queue.put((protocol.PONG, payload))
                elif msg_type == protocol.DISCONNECT:
                    break
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            self.stop.set()
            self._notify_closed(from_thread=True)

    def _send_loop(self):
        while not self.stop.is_set():
            try:
                msg_type, payload = self.send_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.session.channel.send(msg_type, payload)
            except (OSError, ValueError):
                break
        self.stop.set()

    def _notify_closed(self, from_thread=False):
        """on_closed() doit etre appele UNE fois, sur le fil interface."""
        if self._closed_notified.is_set() or not self.on_closed:
            return
        self._closed_notified.set()
        if from_thread:
            try:
                self.window.after(0, self.on_closed)
            except Exception:
                pass
        else:
            self.on_closed()

    def close(self):
        if self.stop.is_set():
            self._notify_closed()
            return
        self.stop.set()
        self.viewer.destroy()
        try:
            self.session.channel.send(protocol.DISCONNECT,
                                      {"reason": "client_closed"})
        except (OSError, ValueError):
            pass
        self.session.channel.close()
        self._notify_closed()


# =====================================================================
#  Interface en ligne de commande
# =====================================================================

def run(args):
    try:
        session = connect(args.host, args.port, args.via, args.name,
                          status_cb=lambda m: print("[client] %s" % m),
                          quality=args.quality)
    except SessionRejected as exc:
        sys.exit("[client] session refusee : %s" % exc)
    except handshake.HandshakeError as exc:
        sys.exit("[client] %s" % exc)

    if not HAVE_UI:
        sys.exit("interface indisponible : pip install pillow (+ tkinter)")

    root = tk.Tk()
    root.title("rdlab - %s (%s)" % (args.host, session.core["machine_id"]))
    root.geometry("1024x640")
    root.configure(bg="#0d0d12")
    status_var = tk.StringVar()
    frame = tk.Frame(root, bg="#0d0d12")
    frame.pack(fill="both", expand=True)
    tk.Label(root, textvariable=status_var, anchor="w", bg="#15151c",
             fg="#8ab4f8", font=("Consolas", 9)).pack(fill="x")
    ctrl = ViewerController(frame, session, status_var,
                            on_closed=root.quit)
    root.protocol("WM_DELETE_WINDOW", ctrl.close)
    try:
        root.mainloop()
    finally:
        ctrl.close()
        print("[client] termine.")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="rdlab.client",
        description="Visionneuse du laboratoire de bureau a distance.")
    p.add_argument("host",
                   help="adresse IP de l hote (mode direct), ou son "
                        "identifiant machine a 9 chiffres (avec --via)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--via", metavar="ANTENNE[:PORT]",
                   help="passer par une antenne (VPS) : 'host' devient alors "
                        "l identifiant machine")
    p.add_argument("--name", default=socket.gethostname(),
                   help="nom affiche a l hote lors de la demande de consentement")
    p.add_argument("--quality", choices=["low", "balanced", "high"])
    run(p.parse_args(argv))


if __name__ == "__main__":
    main()
