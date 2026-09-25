"""L'ANTENNE : serveur de rendez-vous + relais, a heberger sur un VPS.

    python -m rdlab.rendezvous --bind 0.0.0.0 --port 7800

Il remplit les deux roles decrits dans PROTOCOL.md :

  RENDEZ-VOUS  : un annuaire  identifiant_machine -> connexion en attente.
                 L'hote s'y annonce en SORTANT ; le client demande
                 "ou est 312 373 824 ?". Aucun port a ouvrir chez eux.

  RELAIS       : une fois les deux pairs presents, le serveur recopie
                 les octets d'un socket vers l'autre. Rien de plus.

CE QUE CE SERVEUR NE PEUT PAS FAIRE, ET C'EST LE POINT ESSENTIEL :

  Il ne voit QUE du chiffre. Les cles de session sont negociees en
  X25519 *entre les deux pairs*, apres que le tuyau est etabli ; elles
  n'atteignent jamais le relais. Il ne peut pas non plus se faire passer
  pour l'hote : la poignee de main exige une signature Ed25519 dont il
  n'a pas la cle privee. Un relais compromis peut couper la session ou
  la refuser -- il ne peut ni la lire ni l'usurper.

  Il voit en revanche des METADONNEES : qui parle a qui, quand, combien
  d'octets. C'est irreductible pour un relais, et c'est la raison pour
  laquelle la perforation de NAT (connexion directe) reste preferable
  quand elle marche.

POURQUOI L'ENREGISTREMENT EST SIGNE :

  Sans preuve, n'importe qui pourrait s'annoncer sous l'identifiant de
  ta machine et intercepter les demandes de connexion. Il ne pourrait
  pas dechiffrer (pas la cle privee), mais il pourrait faire un deni de
  service. On exige donc une signature sur un defi du serveur.

  Le serveur applique aussi du TOFU : le premier a enregistrer un
  identifiant fixe la cle publique associee. Une cle differente plus
  tard est refusee (--reset-registry pour repartir de zero).

ARCHITECTURE DES CONNEXIONS (3 sockets par session) :

    HOTE                          VPS                        CLIENT
      |--- (1) controle, permanent -->|                          |
      |    REG + signature            |                          |
      |                               |<-- (2) CONNECT_REQUEST --|
      |<-- (3) SESSION_OFFER ticket --|      (client en attente)  |
      |--- (4) NOUVELLE connexion --->|                          |
      |    CLAIM ticket               |                          |
      |                          [appariement]                   |
      |<====== (5) recopie d'octets, chiffres de bout en bout ===>|

Le canal de controle (1) reste ouvert entre les sessions ; les donnees
passent par une connexion neuve (4). C'est ce qui permet de refuser une
session sans casser l'enregistrement, et d'en enchainer plusieurs.
"""

import argparse
import json
import os
import socket
import threading
import time

from cryptography.exceptions import InvalidSignature

from . import identity, protocol

# --- messages du protocole d'antenne (espace 0x60+, distinct du reste)
REG_REQUEST = 0x60      # hote -> antenne : identifiant + cle publique
REG_CHALLENGE = 0x61    # antenne -> hote : defi aleatoire
REG_PROOF = 0x62        # hote -> antenne : signature du defi
REG_OK = 0x63           # antenne -> hote : enregistre
CONNECT_REQUEST = 0x64  # client -> antenne : "ou est <identifiant> ?"
SESSION_OFFER = 0x65    # antenne -> hote : "un client attend, ticket X"
CLAIM = 0x66            # hote -> antenne : "voici le ticket X" (conn. neuve)
PEER_READY = 0x67       # antenne -> les deux : le tuyau est ouvert
RELAY_ERROR = 0x68      # antenne -> x : motif de refus
RELAY_PING = 0x69       # antenne -> hote : sonde de vivacite

REG_CONTEXT = b"rdlab-relay-registration-v1"
DEFAULT_PORT = 7800
TICKET_TTL = 30.0
PAIR_TIMEOUT = 25.0
CONTROL_PING = 30.0
REGISTRY_FILE = "relay_registry.json"


def normalize_id(machine_id):
    """'312 373 824', '312-373-824' et '312373824' designent la meme machine."""
    return "".join(c for c in str(machine_id) if c.isdigit())


def _error(sock, reason):
    try:
        protocol.write_frame_plain(sock, RELAY_ERROR, {"reason": reason})
    except OSError:
        pass


# =====================================================================
#  Cote VPS
# =====================================================================

class Registration:
    def __init__(self, machine_id, device_pub, control, addr):
        self.machine_id = machine_id
        self.device_pub = device_pub
        self.control = control
        self.addr = addr
        self.since = time.time()
        self.lock = threading.Lock()   # un seul ecrivain sur le canal

    def offer(self, ticket):
        with self.lock:
            protocol.write_frame_plain(self.control, SESSION_OFFER,
                                       {"ticket": ticket})


class Pending:
    def __init__(self, client_sock, client_addr):
        self.client_sock = client_sock
        self.client_addr = client_addr
        self.host_sock = None
        self.ready = threading.Event()
        self.created = time.time()


class RelayServer:
    def __init__(self, bind="0.0.0.0", port=DEFAULT_PORT, max_sessions=4,
                 registry_path=None, quiet=False):
        self.bind = bind
        self.port = port
        self.max_sessions = max_sessions
        self.quiet = quiet
        self.registrations = {}
        self.pending = {}
        self.sessions = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.registry_path = registry_path or os.path.join(
            identity._ensure_dir(), REGISTRY_FILE)
        self._registry = {}
        if os.path.exists(self.registry_path):
            with open(self.registry_path, "r", encoding="utf-8") as fh:
                self._registry = json.load(fh)
        self.bound_port = None

    def log(self, fmt, *args):
        if not self.quiet:
            print("[antenne %s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    # -- TOFU cote serveur -------------------------------------------
    def _check_registry(self, machine_id, device_pub_b64):
        known = self._registry.get(machine_id)
        if known is None:
            self._registry[machine_id] = device_pub_b64
            with open(self.registry_path, "w", encoding="utf-8") as fh:
                json.dump(self._registry, fh, indent=2)
            return True
        return known == device_pub_b64

    # -- enregistrement d'un hote ------------------------------------
    def _handle_registration(self, sock, addr, req):
        import base64
        machine_id = normalize_id(req.get("machine_id", ""))
        device_pub = base64.b64decode(req.get("device_pub", ""))

        if len(machine_id) != 9 or len(device_pub) != 32:
            _error(sock, "invalid_registration")
            return False
        # l'identifiant doit reellement deriver de la cle annoncee
        if normalize_id(identity.machine_id(device_pub)) != machine_id:
            _error(sock, "id_does_not_match_key")
            self.log("REFUS %s : identifiant incoherent avec la cle", addr[0])
            return False
        if not self._check_registry(machine_id, req["device_pub"]):
            _error(sock, "id_bound_to_another_key")
            self.log("REFUS %s : identifiant %s deja lie a une autre cle",
                     addr[0], machine_id)
            return False

        nonce = os.urandom(32)
        protocol.write_frame_plain(sock, REG_CHALLENGE,
                                   {"nonce": base64.b64encode(nonce).decode()})
        msg_type, payload = protocol.read_frame_plain(sock)
        if msg_type != REG_PROOF:
            _error(sock, "expected_proof")
            return False
        signature = base64.b64decode(protocol.as_json(payload)["signature"])
        try:
            identity.verify_signature(
                device_pub, signature, REG_CONTEXT + nonce)
        except InvalidSignature:
            _error(sock, "bad_signature")
            self.log("REFUS %s : signature d enregistrement invalide", addr[0])
            return False

        with self._lock:
            old = self.registrations.pop(machine_id, None)
            self.registrations[machine_id] = Registration(
                machine_id, device_pub, sock, addr)
        if old:
            try:
                old.control.close()
            except OSError:
                pass
        protocol.write_frame_plain(sock, REG_OK, {"machine_id": machine_id})
        self.log("ENREGISTRE %s depuis %s", machine_id, addr[0])

        # le canal de controle reste ouvert ; on le sonde periodiquement
        try:
            while not self._stop.is_set():
                time.sleep(CONTROL_PING)
                reg = self.registrations.get(machine_id)
                if reg is None or reg.control is not sock:
                    break
                with reg.lock:
                    protocol.write_frame_plain(sock, RELAY_PING, {})
        except OSError:
            pass
        finally:
            with self._lock:
                if self.registrations.get(machine_id) is not None and \
                        self.registrations[machine_id].control is sock:
                    del self.registrations[machine_id]
            self.log("DECONNEXION du canal de controle %s", machine_id)
        return False

    # -- demande d'un client -----------------------------------------
    def _handle_connect(self, sock, addr, req):
        machine_id = normalize_id(req.get("machine_id", ""))
        reg = self.registrations.get(machine_id)
        if reg is None:
            _error(sock, "host_not_registered")
            self.log("demande de %s pour %s : hote absent", addr[0], machine_id)
            return False
        with self._lock:
            if self.sessions >= self.max_sessions:
                _error(sock, "relay_busy")
                return False
            ticket = os.urandom(16).hex()
            self.pending[ticket] = Pending(sock, addr)

        try:
            reg.offer(ticket)
        except OSError:
            self.pending.pop(ticket, None)
            _error(sock, "host_unreachable")
            return False

        self.log("appariement demande : %s <- %s (ticket %s...)",
                 machine_id, addr[0], ticket[:8])
        entry = self.pending.get(ticket)
        if not entry.ready.wait(PAIR_TIMEOUT):
            self.pending.pop(ticket, None)
            _error(sock, "host_did_not_answer")
            self.log("ticket %s... expire", ticket[:8])
            return False
        self.pending.pop(ticket, None)
        self._pipe(entry.client_sock, entry.host_sock, machine_id, addr)
        return True     # sockets fermes par _pipe

    # -- reclamation par l'hote --------------------------------------
    def _handle_claim(self, sock, addr, req):
        ticket = str(req.get("ticket", ""))
        entry = self.pending.get(ticket)
        if entry is None:
            _error(sock, "unknown_or_expired_ticket")
            return False
        if time.time() - entry.created > TICKET_TTL:
            self.pending.pop(ticket, None)
            _error(sock, "ticket_expired")
            return False
        entry.host_sock = sock
        entry.ready.set()
        return True     # remis au fil du client, ne pas fermer ici

    # -- recopie d'octets --------------------------------------------
    def _pipe(self, a, b, machine_id, client_addr):
        """Le coeur du relais : deux copies aveugles en sens inverse."""
        with self._lock:
            self.sessions += 1
        counters = [0, 0]
        started = time.time()
        try:
            protocol.write_frame_plain(a, PEER_READY, {"role": "client"})
            protocol.write_frame_plain(b, PEER_READY, {"role": "host"})
        except OSError:
            pass

        def copy(src, dst, idx):
            try:
                while True:
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
                    counters[idx] += len(chunk)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t1 = threading.Thread(target=copy, args=(a, b, 0), daemon=True)
        t2 = threading.Thread(target=copy, args=(b, a, 1), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass
        with self._lock:
            self.sessions -= 1
        self.log("session %s <-> %s terminee : %.1f s, %d ko c->h, %d ko h->c",
                 machine_id, client_addr[0], time.time() - started,
                 counters[0] // 1024, counters[1] // 1024)

    # -- aiguillage ---------------------------------------------------
    def _serve_connection(self, sock, addr):
        handed_off = False
        try:
            sock.settimeout(30)
            msg_type, payload = protocol.read_frame_plain(sock)
            req = protocol.as_json(payload) if payload else {}
            sock.settimeout(None)
            if msg_type == REG_REQUEST:
                handed_off = self._handle_registration(sock, addr, req)
            elif msg_type == CONNECT_REQUEST:
                handed_off = self._handle_connect(sock, addr, req)
            elif msg_type == CLAIM:
                handed_off = self._handle_claim(sock, addr, req)
            else:
                _error(sock, "unexpected_message")
        except (OSError, ValueError, KeyError) as exc:
            self.log("connexion %s rejetee : %s", addr[0], exc)
        finally:
            if not handed_off:
                try:
                    sock.close()
                except OSError:
                    pass

    def serve_forever(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(32)
        self.bound_port = srv.getsockname()[1]
        self.log("ecoute sur %s:%d (max %d sessions simultanees)",
                 self.bind, self.bound_port, self.max_sessions)
        self.log("registre TOFU : %s", self.registry_path)
        try:
            while not self._stop.is_set():
                sock, addr = srv.accept()
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(target=self._serve_connection,
                                 args=(sock, addr), daemon=True).start()
        except (KeyboardInterrupt, OSError):
            pass
        finally:
            self._stop.set()
            srv.close()
            self.log("arret")

    def stop(self):
        self._stop.set()


# =====================================================================
#  Cote hote : s'annoncer a l'antenne et y recevoir des sessions
# =====================================================================

class RelayAgent:
    """Maintient le canal de controle et fournit des sockets de session."""

    def __init__(self, relay_host, relay_port, device):
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.device = device
        self.control = None

    def register(self):
        import base64
        sock = socket.create_connection((self.relay_host, self.relay_port),
                                        timeout=20)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        protocol.write_frame_plain(sock, REG_REQUEST, {
            "machine_id": normalize_id(self.device.machine_id),
            "device_pub": base64.b64encode(self.device.public_bytes).decode(),
        })
        msg_type, payload = protocol.read_frame_plain(sock)
        if msg_type == RELAY_ERROR:
            raise RuntimeError("antenne : %s"
                               % protocol.as_json(payload).get("reason"))
        if msg_type != REG_CHALLENGE:
            raise RuntimeError("antenne : reponse inattendue")
        nonce = base64.b64decode(protocol.as_json(payload)["nonce"])
        signature = self.device.sign(REG_CONTEXT + nonce)
        protocol.write_frame_plain(sock, REG_PROOF, {
            "signature": base64.b64encode(signature).decode()})
        msg_type, payload = protocol.read_frame_plain(sock)
        if msg_type != REG_OK:
            raise RuntimeError("antenne : enregistrement refuse (%s)"
                               % protocol.name(msg_type))
        sock.settimeout(None)
        self.control = sock
        return True

    def accept(self):
        """Bloque jusqu a ce qu un client demande une session.

        Retourne un socket DEJA apparie au client, sur lequel la poignee
        de main rdlab habituelle se deroule sans aucune modification."""
        while True:
            msg_type, payload = protocol.read_frame_plain(self.control)
            if msg_type == RELAY_PING:
                continue
            if msg_type == RELAY_ERROR:
                raise RuntimeError("antenne : %s"
                                   % protocol.as_json(payload).get("reason"))
            if msg_type != SESSION_OFFER:
                continue
            ticket = protocol.as_json(payload)["ticket"]
            data = socket.create_connection((self.relay_host, self.relay_port),
                                            timeout=20)
            data.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            protocol.write_frame_plain(data, CLAIM, {"ticket": ticket})
            msg_type, payload = protocol.read_frame_plain(data)
            if msg_type != PEER_READY:
                data.close()
                continue
            data.settimeout(None)
            return data, ("via-relais:%s" % self.relay_host, self.relay_port)

    def close(self):
        if self.control:
            try:
                self.control.close()
            except OSError:
                pass


# =====================================================================
#  Cote client : demander une machine par son identifiant
# =====================================================================

def relay_connect(relay_host, relay_port, machine_id, timeout=30):
    """Retourne un socket relie a l hote, pret pour client_handshake()."""
    sock = socket.create_connection((relay_host, relay_port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    protocol.write_frame_plain(sock, CONNECT_REQUEST,
                               {"machine_id": normalize_id(machine_id)})
    sock.settimeout(PAIR_TIMEOUT + 10)
    msg_type, payload = protocol.read_frame_plain(sock)
    if msg_type == RELAY_ERROR:
        sock.close()
        raise RuntimeError("antenne : %s"
                           % protocol.as_json(payload).get("reason"))
    if msg_type != PEER_READY:
        sock.close()
        raise RuntimeError("antenne : reponse inattendue %s"
                           % protocol.name(msg_type))
    sock.settimeout(None)
    return sock


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="rdlab.rendezvous",
        description="Antenne rdlab : rendez-vous + relais (a heberger sur "
                    "votre VPS).")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--max-sessions", type=int, default=4,
                   help="garde-fou de bande passante du VPS")
    p.add_argument("--reset-registry", action="store_true",
                   help="oublie les liaisons identifiant -> cle publique")
    args = p.parse_args(argv)

    server = RelayServer(args.bind, args.port, args.max_sessions)
    if args.reset_registry:
        server._registry = {}
        with open(server.registry_path, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        print("registre remis a zero.")
    server.serve_forever()


if __name__ == "__main__":
    main()
