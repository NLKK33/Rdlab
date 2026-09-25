"""Le MOTEUR de l'hote : la machine dont l'ecran est partage.

Ce module ne sait pas s'il est pilote par un terminal ou par une
interface graphique. Il parle a un objet `HostHooks` :

    moteur  --status()---->  "en attente de connexion"
            --event()----->  session_start, session_stats, auth_failed...
            --consent()--->  BLOQUE jusqu'a ce qu'un HUMAIN reponde

C'est cette inversion qui permet a rdlab.app d'afficher une vraie boite
de dialogue de consentement, la ou la version console pose la question
dans le terminal. Le moteur, lui, est identique : une seule
implementation du protocole, deux presentations.

Architecture interne (3 fils d'execution par session) :

    [fil capture]  --queue(2)-->  [fil emission]  ---> reseau
                                                        |
    [fil session]  <-------------------------------- reseau
       applique les evenements souris/clavier

La file est volontairement minuscule (2 images). Quand elle est pleine,
on JETTE l'image : mieux vaut sauter une image que prendre du retard.
"""

import argparse
import queue
import socket
import sys
import threading
import time

from . import (auth, capture, handshake, identity, inject, netadapt,
               protocol, rendezvous, session_log)

BANNER = r"""
+--------------------------------------------------------------+
|  rdlab - HOTE : cet ordinateur peut etre observe a distance   |
|  Laboratoire pedagogique. Reseau de test uniquement.          |
+--------------------------------------------------------------+"""

CONSENT_TIMEOUT = 60


# =====================================================================
#  Interface moteur <-> presentation
# =====================================================================

class HostHooks:
    """A sous-classer par une interface. Par defaut : silencieux."""

    def status(self, message):
        """Etat courant, une ligne, remplacable a l'ecran."""

    def event(self, kind, **fields):
        """Evenement ponctuel, a journaliser ou afficher."""

    def consent(self, request):
        """DOIT bloquer jusqu'a la reponse d'un humain, et RETOURNER
        False par defaut. Une expiration qui accepterait serait un
        defaut de securite : l'absence de reponse signifie non."""
        return False


class ConsoleHooks(HostHooks):
    def status(self, message):
        print("[hote] %s" % message)

    def event(self, kind, **fields):
        if kind == "session_stats":
            print("[session] %s | images=%d tuiles=%d ko=%d ev=%d"
                  % (fields["quality"], fields["frames"], fields["tiles"],
                     fields["kbytes"], fields["events"]))
        elif kind in ("auth_failed", "error", "handshake_failed"):
            print("[hote] %s : %s" % (kind, fields.get("detail", "")))

    def consent(self, request):
        print("\n" + "=" * 62)
        print("DEMANDE DE CONNEXION")
        print("  client   : %s" % request["client"])
        print("  adresse  : %s" % request["peer"])
        print("  code de verification (a comparer avec le client) : %s"
              % request["sas"])
        print("=" * 62)
        answer = {"value": None}

        def _read():
            try:
                answer["value"] = input("Autoriser cette session ? [o/N] ")
            except EOFError:
                answer["value"] = ""

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(CONSENT_TIMEOUT)
        ok = (answer["value"] or "").strip().lower() in ("o", "oui", "y", "yes")
        print("-> %s" % ("ACCEPTE" if ok else "REFUSE"))
        return ok


# =====================================================================
#  Une session en cours
# =====================================================================

class HostSession:
    def __init__(self, channel, source, injector, controller, permissions,
                 hooks):
        self.channel = channel
        self.source = source
        self.injector = injector
        self.ctl = controller
        self.permissions = permissions
        self.hooks = hooks
        self.frames = queue.Queue(maxsize=2)
        self.running = threading.Event()
        self.running.set()
        self.stats = {"frames": 0, "tiles": 0, "bytes": 0, "events": 0}
        self._ping_sent = {}

    def stop(self):
        """Coupe-circuit, appelable depuis l'interface."""
        self.running.clear()
        try:
            self.channel.send(protocol.DISCONNECT, {"reason": "host_stopped"})
        except (OSError, ValueError):
            pass
        self.channel.close()

    # -- fil 1 : capture + encodage ---------------------------------
    def _capture_loop(self):
        while self.running.is_set():
            started = time.monotonic()
            try:
                result = self.source.next_frame(quality=self.ctl.quality)
            except Exception as exc:
                self.hooks.event("error", detail="capture: %s" % exc)
                break
            if result is not None:
                payload, tiles, nbytes = result
                try:
                    self.frames.put_nowait((payload, tiles, nbytes))
                except queue.Full:
                    self.ctl.observe_drop()  # on jette, on ne met pas en file
            budget = 1.0 / max(1.0, self.ctl.fps)
            time.sleep(max(0.0, budget - (time.monotonic() - started)))

    # -- fil 2 : emission -------------------------------------------
    def _send_loop(self):
        last_ping = 0.0
        while self.running.is_set():
            now = time.monotonic()
            if now - last_ping > 2.0:
                last_ping = now
                token = int(now * 1000) & 0xFFFFFFFF
                self._ping_sent[token] = now
                try:
                    self.channel.send(protocol.PING, {"token": token})
                except (OSError, ValueError):
                    break
            try:
                payload, tiles, nbytes = self.frames.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self.channel.send(protocol.SCREEN_FRAME, payload)
            except (OSError, ValueError):
                break
            self.stats["frames"] += 1
            self.stats["tiles"] += tiles
            self.stats["bytes"] += nbytes
            self.ctl.observe_sent(nbytes)
            self.ctl.adjust(self.frames.qsize())

    # -- fil principal : reception ----------------------------------
    def run(self):
        self.channel.send(protocol.SCREEN_INFO, {
            "width": self.source.width, "height": self.source.height,
            "tile": self.source.tile})
        self.channel.send(protocol.SESSION_ACCEPTED, self.permissions)

        threads = [threading.Thread(target=self._capture_loop, daemon=True),
                   threading.Thread(target=self._send_loop, daemon=True)]
        for t in threads:
            t.start()

        last_report = time.monotonic()
        try:
            while self.running.is_set():
                msg_type, payload = self.channel.recv()
                if msg_type == protocol.MOUSE_EVENT:
                    self.injector.mouse_event(protocol.as_json(payload))
                    self.stats["events"] += 1
                elif msg_type == protocol.KEY_EVENT:
                    self.injector.key_event(protocol.as_json(payload))
                    self.stats["events"] += 1
                elif msg_type == protocol.PONG:
                    token = protocol.as_json(payload).get("token")
                    sent = self._ping_sent.pop(token, None)
                    if sent:
                        self.ctl.observe_rtt((time.monotonic() - sent) * 1000)
                elif msg_type == protocol.PING:
                    self.channel.send(protocol.PONG, payload)
                elif msg_type == protocol.QUALITY_HINT:
                    self.ctl.apply_hint(protocol.as_json(payload).get("level"))
                elif msg_type == protocol.DISCONNECT:
                    self.hooks.status("le client a ferme la session")
                    break
                now = time.monotonic()
                if now - last_report > 2.0:
                    last_report = now
                    self.hooks.event("session_stats",
                                     quality=self.ctl.stats(),
                                     frames=self.stats["frames"],
                                     tiles=self.stats["tiles"],
                                     kbytes=self.stats["bytes"] // 1024,
                                     events=self.stats["events"])
        except (OSError, ValueError, ConnectionError) as exc:
            self.hooks.status("fin de session : %s" % exc)
        finally:
            self.running.clear()
            self.injector.release_all()  # ne jamais laisser une touche bloquee
            for t in threads:
                t.join(timeout=1.0)


# =====================================================================
#  Le service
# =====================================================================

class HostOptions:
    def __init__(self, bind="127.0.0.1", port=7700, relay=None, monitor=1,
                 view_only=False):
        self.bind = bind
        self.port = port
        self.relay = relay
        self.monitor = monitor
        self.view_only = view_only


def split_endpoint(value, default_port):
    """'vps.exemple.net' ou 'vps.exemple.net:7800' -> (hote, port)."""
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host, int(port)
    return value, default_port


class HostService:
    """Le moteur. start() bloque ; stop() l'interrompt depuis un autre fil."""

    def __init__(self, device, creds, options, hooks=None):
        self.device = device
        self.creds = creds
        self.options = options
        self.hooks = hooks or HostHooks()
        self._stop = threading.Event()
        self._srv = None
        self._agent = None
        self._session = None

    # -- controle ----------------------------------------------------
    def stop(self):
        self._stop.set()
        if self._session:
            self._session.stop()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass
        if self._agent:
            self._agent.close()

    def stop_session(self):
        """Termine la session en cours sans arreter le service."""
        if self._session:
            self._session.stop()

    @property
    def running(self):
        return not self._stop.is_set()

    # -- une connexion -----------------------------------------------
    def handle_client(self, sock, peer):
        peer_label = "%s:%s" % peer
        session_log.log("connection_attempt", peer=peer_label)
        channel = None
        try:
            channel, client_info, sas, binding = handshake.host_handshake(
                sock, self.device)
            self.hooks.status("poignee de main avec %r (code %s)"
                              % (client_info["name"], sas))

            if not handshake.host_authenticate(channel, binding, self.creds):
                # On journalise et on previent l'interface AVANT de repondre :
                # la trace d'une tentative echouee ne doit pas dependre du
                # fait que le client reste connecte pour la lire.
                session_log.log("auth_failed", peer=peer_label,
                                client=client_info["name"])
                self.hooks.event("auth_failed", detail="mot de passe refuse",
                                 peer=peer_label, client=client_info["name"])
                channel.send(protocol.SESSION_REJECTED,
                             {"reason": "bad_password"})
                return
            session_log.log("auth_ok", peer=peer_label,
                            client=client_info["name"])

            if self.creds.unattended:
                self.hooks.status("acces sans surveillance : session acceptee")
                session_log.log("consent", mode="unattended", granted=True,
                                peer=peer_label)
            else:
                channel.send(protocol.CONSENT_PENDING,
                             {"message": "en attente de l accord de l hote"})
                granted = self.hooks.consent({
                    "client": client_info["name"], "peer": peer_label,
                    "sas": sas, "view_only": self.options.view_only})
                session_log.log("consent", mode="interactive", granted=granted,
                                peer=peer_label)
                if not granted:
                    self.hooks.event("consent_denied", peer=peer_label)
                    channel.send(protocol.SESSION_REJECTED,
                                 {"reason": "consent_denied"})
                    return

            source = capture.ScreenSource(monitor=self.options.monitor)
            allow_input = (self.creds.data.get("allow_input", True)
                           and not self.options.view_only)
            injector = inject.InputInjector(source.width, source.height,
                                            allow_input=allow_input)
            permissions = {"allow_input": allow_input, "allow_clipboard": False,
                           "width": source.width, "height": source.height}
            session_log.log("session_start", peer=peer_label,
                            client=client_info["name"], allow_input=allow_input)
            self.hooks.event("session_start", peer=peer_label,
                             client=client_info["name"],
                             allow_input=allow_input,
                             width=source.width, height=source.height)
            started = time.monotonic()
            self._session = HostSession(channel, source, injector,
                                        netadapt.QualityController(),
                                        permissions, self.hooks)
            try:
                self._session.run()
            finally:
                self._session = None
                source.close()
                duration = round(time.monotonic() - started, 1)
                session_log.log("session_end", peer=peer_label,
                                duration_s=duration)
                self.hooks.event("session_end", peer=peer_label,
                                 duration_s=duration)
        except handshake.HandshakeError as exc:
            session_log.log("handshake_failed", peer=peer_label, error=str(exc))
            self.hooks.event("handshake_failed", detail=str(exc),
                             peer=peer_label)
        except (ConnectionError, OSError, ValueError) as exc:
            self.hooks.status("connexion interrompue : %s" % exc)
        except Exception as exc:
            # Une session qui echoue ne doit jamais faire tomber l'agent :
            # sinon, en mode antenne, une seule requete malformee suffirait
            # a desenregistrer la machine.
            session_log.log("session_error", peer=peer_label, error=repr(exc))
            self.hooks.event("error", detail=repr(exc))
        finally:
            if channel:
                channel.close()
            else:
                try:
                    sock.close()
                except OSError:
                    pass

    # -- mode direct (LAN) -------------------------------------------
    def _serve_direct(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.options.bind, self.options.port))
        self._srv.listen(4)
        session_log.log("host_started", mode="direct", bind=self.options.bind,
                        port=self.options.port,
                        machine_id=self.device.machine_id)
        self.hooks.event("listening", bind=self.options.bind,
                         port=self.options.port)
        self.hooks.status("en attente sur %s:%d"
                          % (self.options.bind, self.options.port))
        try:
            while not self._stop.is_set():
                sock, peer = self._srv.accept()
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.hooks.status("connexion entrante de %s:%s" % peer)
                self.handle_client(sock, peer)
                if not self._stop.is_set():
                    self.hooks.status("en attente sur %s:%d"
                                      % (self.options.bind, self.options.port))
        finally:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None

    # -- mode antenne (VPS) ------------------------------------------
    def _serve_via_relay(self):
        """On s'annonce en SORTANT sur le VPS : aucun port ouvert ici."""
        relay_host, relay_port = split_endpoint(self.options.relay,
                                                rendezvous.DEFAULT_PORT)
        backoff = 2.0
        while not self._stop.is_set():
            self._agent = rendezvous.RelayAgent(relay_host, relay_port,
                                                self.device)
            try:
                self._agent.register()
                session_log.log("relay_registered", relay=relay_host,
                                port=relay_port,
                                machine_id=self.device.machine_id)
                self.hooks.event("registered", relay=relay_host,
                                 port=relay_port)
                self.hooks.status("annonce sur %s:%d - en attente"
                                  % (relay_host, relay_port))
                backoff = 2.0
                while not self._stop.is_set():
                    sock, peer = self._agent.accept()
                    self.hooks.status("session proposee par l antenne")
                    self.handle_client(sock, peer)
                    if not self._stop.is_set():
                        self.hooks.status("annonce sur %s:%d - en attente"
                                          % (relay_host, relay_port))
            except (OSError, RuntimeError, ValueError) as exc:
                if self._stop.is_set():
                    break
                session_log.log("relay_disconnected", error=str(exc))
                self.hooks.event("relay_error", detail=str(exc),
                                 retry_in=backoff)
                self.hooks.status("antenne injoignable (%s) - nouvel essai "
                                  "dans %.0f s" % (exc, backoff))
                self._stop.wait(backoff)
                backoff = min(60.0, backoff * 2)
            finally:
                if self._agent:
                    self._agent.close()

    def start(self):
        """Bloque jusqu'a stop(). A lancer dans un fil depuis une interface."""
        if not self.creds.has_password:
            raise RuntimeError("aucun mot de passe defini")
        try:
            if self.options.relay:
                self._serve_via_relay()
            else:
                self._serve_direct()
        finally:
            self._stop.set()
            session_log.log("host_stopped")
            self.hooks.event("stopped")
            self.hooks.status("service arrete")


# =====================================================================
#  Interface en ligne de commande
# =====================================================================

def serve(args):
    device = identity.DeviceIdentity.load_or_create()
    creds = auth.CredentialStore()
    if not creds.has_password:
        sys.exit("Aucun mot de passe defini. Lancez d'abord :\n"
                 "    python -m rdlab.host --set-password")

    print(BANNER)
    print("  identifiant machine : %s" % device.machine_id)
    print("  empreinte (a verifier cote client) :\n     %s" % device.fingerprint)
    if args.relay:
        print("  mode                : via l antenne %s" % args.relay)
        print("                        (connexion SORTANTE, aucun port ouvert)")
    else:
        print("  mode                : direct, ecoute sur %s:%d"
              % (args.bind, args.port))
    print("  acces sans surveillance : %s"
          % ("ACTIF" if creds.unattended else "inactif (consentement requis)"))
    print("  journal             : rdlab-data/sessions.log")
    print("  Ctrl+C pour tout arreter.\n")

    options = HostOptions(bind=args.bind, port=args.port, relay=args.relay,
                          monitor=args.monitor, view_only=args.view_only)
    service = HostService(device, creds, options, ConsoleHooks())
    try:
        service.start()
    except KeyboardInterrupt:
        print("\n[hote] arret demande")
        service.stop()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="rdlab.host",
        description="Agent hote du laboratoire de bureau a distance.")
    p.add_argument("--bind", default="127.0.0.1",
                   help="adresse d ecoute (127.0.0.1 par defaut ; mettez "
                        "l IP LAN pour accepter l autre machine)")
    p.add_argument("--port", type=int, default=7700)
    p.add_argument("--relay", metavar="HOTE[:PORT]",
                   help="s annoncer sur une antenne (VPS) au lieu d ecouter "
                        "un port ; connexion sortante uniquement")
    p.add_argument("--monitor", type=int, default=1)
    p.add_argument("--view-only", action="store_true",
                   help="observation seule : aucune entree n est appliquee")
    p.add_argument("--set-password", action="store_true")
    p.add_argument("--clear-password", action="store_true")
    p.add_argument("--unattended", choices=["on", "off"],
                   help="acces sans surveillance (desactive par defaut)")
    p.add_argument("--status", action="store_true")
    p.add_argument("--log", type=int, metavar="N",
                   help="affiche les N dernieres entrees du journal")
    args = p.parse_args(argv)

    creds = auth.CredentialStore()
    device = identity.DeviceIdentity.load_or_create()
    did_admin = False

    if args.set_password:
        import getpass
        pw = getpass.getpass("Nouveau mot de passe d acces : ")
        if pw != getpass.getpass("Confirmez : "):
            sys.exit("les mots de passe different")
        creds.set_password(pw)
        session_log.log("password_changed")
        print("Mot de passe enregistre.")
        did_admin = True
    if args.clear_password:
        creds.clear_password()
        session_log.log("password_cleared")
        print("Mot de passe efface, acces sans surveillance desactive.")
        did_admin = True
    if args.unattended:
        if args.unattended == "on":
            creds.enable_unattended()
            print("Acces sans surveillance ACTIVE. Desactivation :\n"
                  "    python -m rdlab.host --unattended off")
        else:
            creds.disable_unattended()
            print("Acces sans surveillance desactive.")
        session_log.log("unattended_changed", value=args.unattended)
        did_admin = True
    if args.log:
        for entry in session_log.tail(args.log):
            print(entry)
        did_admin = True
    if args.status:
        print("identifiant machine : %s" % device.machine_id)
        print("empreinte           : %s" % device.fingerprint)
        print("mot de passe defini : %s" % creds.has_password)
        print("sans surveillance   : %s" % creds.unattended)
        did_admin = True

    if did_admin:
        return
    serve(args)


if __name__ == "__main__":
    main()
