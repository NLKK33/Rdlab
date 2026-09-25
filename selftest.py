"""Verification des etapes 1, 2 et 6 SANS ecran ni deuxieme machine.

    python selftest.py

Teste, sur une paire de sockets locales :
  - le cadrage des messages (protocol) ;
  - le canal chiffre AES-GCM et sa resistance au rejeu ;
  - la poignee de main complete + signature de l'hote + SAS identique ;
  - l'authentification par defi/reponse, y compris son echec sur un
    mauvais mot de passe et sur une liaison de canal falsifiee (MITM).

Les modules capture/inject ne sont pas testes ici : ils demandent un
vrai bureau graphique.
"""

import os
import socket
import sys
import tempfile
import threading
import time

os.environ.setdefault("RDLAB_DATA", os.path.join(tempfile.gettempdir(),
                                                 "rdlab-selftest"))

from rdlab import auth, crypto, handshake, identity, protocol  # noqa: E402
from rdlab import rendezvous  # noqa: E402
from rdlab import capture, client as rdclient, host as rdhost  # noqa: E402

PASSWORD = "mot-de-passe-de-test-42"
_ok = _ko = 0


def check(label, condition):
    global _ok, _ko
    if condition:
        _ok += 1
        print("  [ok]   %s" % label)
    else:
        _ko += 1
        print("  [FAIL] %s" % label)


def test_framing():
    print("\n1. Cadrage des messages")
    frame = protocol.encode(protocol.MOUSE_EVENT, {"action": "move", "x": 0.5})
    t, body = protocol.decode(frame)
    check("aller-retour type + payload", t == protocol.MOUSE_EVENT
          and protocol.as_json(body)["x"] == 0.5)
    check("payload binaire brut preserve",
          protocol.decode(protocol.encode(protocol.SCREEN_FRAME,
                                          b"\x00\xff\x10"))[1] == b"\x00\xff\x10")
    check("nom lisible d un type", protocol.name(protocol.PING) == "PING")
    try:
        protocol.encode(protocol.SCREEN_FRAME, b"x" * (protocol.MAX_PAYLOAD + 1))
        check("refus d un payload surdimensionne", False)
    except ValueError:
        check("refus d un payload surdimensionne", True)


def test_secure_channel():
    print("\n2. Canal chiffre")
    a, b = socket.socketpair()
    k1, k2 = os.urandom(32), os.urandom(32)
    left = crypto.SecureChannel(a, k1, k2)
    right = crypto.SecureChannel(b, k2, k1)
    left.send(protocol.KEY_EVENT, {"keysym": "a", "action": "down"})
    t, payload = right.recv()
    check("message chiffre puis dechiffre", t == protocol.KEY_EVENT
          and protocol.as_json(payload)["keysym"] == "a")

    # rejeu : on capture les octets d un message valide, on les laisse
    # passer une fois, puis on les reinjecte. Le compteur de nonce du
    # receveur a avance : la seconde copie ne peut plus se dechiffrer.
    left.send(protocol.PING, {"token": 1})
    raw_len = protocol.recv_exact(b, 4)
    raw = raw_len + protocol.recv_exact(b, int.from_bytes(raw_len, "big"))
    a.sendall(raw)
    check("copie legitime acceptee", right.recv()[0] == protocol.PING)
    a.sendall(raw)                     # injection du doublon
    try:
        right.recv()
        check("un message rejoue est rejete", False)
    except Exception:
        check("un message rejoue est rejete", True)
    a.close()
    b.close()


def test_key_derivation():
    print("\n3. Derivation de cles")
    secret = os.urandom(32)
    th = crypto.transcript_hash(b"hello", b"ack")
    k1 = crypto.derive_keys(secret, th)
    k2 = crypto.derive_keys(secret, th)
    check("deterministe", k1 == k2)
    check("cles distinctes par sens",
          k1["host_to_client"] != k1["client_to_host"])
    k3 = crypto.derive_keys(secret, crypto.transcript_hash(b"hello", b"ack!"))
    check("un transcript modifie change toutes les cles",
          k3["host_to_client"] != k1["host_to_client"])
    check("SAS a 6 chiffres",
          len(crypto.short_auth_string(k1["sas"])) == 6)


def _run_pair(password_used, tamper_binding=False):
    """Deroule une poignee de main complete client/hote sur socketpair."""
    a, b = socket.socketpair()
    device = identity.DeviceIdentity.load_or_create()
    creds = auth.CredentialStore()
    if not creds.has_password:
        creds.set_password(PASSWORD)
    result = {}

    def _host():
        try:
            ch, info, sas, binding = handshake.host_handshake(b, device)
            result["host_sas"] = sas
            result["client_name"] = info["name"]
            result["auth_ok"] = handshake.host_authenticate(ch, binding, creds)
        except Exception as exc:                        # pragma: no cover
            result["host_error"] = repr(exc)

    t = threading.Thread(target=_host)
    t.start()
    try:
        ch, core, sas, binding = handshake.client_handshake(
            a, "client-de-test", lambda core, status, sas: True)
        result["client_sas"] = sas
        result["fingerprint"] = core["fingerprint"]
        if tamper_binding:
            binding = os.urandom(32)   # simule un relais par un tiers
        handshake.client_authenticate(ch, binding, lambda: password_used)
    except Exception as exc:                            # pragma: no cover
        result["client_error"] = repr(exc)
    t.join(timeout=20)
    a.close()
    b.close()
    return result, device


def test_handshake():
    print("\n4. Poignee de main + authentification")
    res, device = _run_pair(PASSWORD)
    check("pas d erreur", "host_error" not in res and "client_error" not in res)
    check("les deux cotes calculent le MEME SAS",
          res.get("host_sas") and res["host_sas"] == res.get("client_sas"))
    check("empreinte transmise = empreinte reelle de la cle",
          res.get("fingerprint") == device.fingerprint)
    check("le nom du client parvient a l hote",
          res.get("client_name") == "client-de-test")
    check("bon mot de passe -> authentification reussie",
          res.get("auth_ok") is True)

    print("\n5. Rejets attendus")
    bad, _ = _run_pair("mauvais-mot-de-passe")
    check("mauvais mot de passe -> refus", bad.get("auth_ok") is False)
    mitm, _ = _run_pair(PASSWORD, tamper_binding=True)
    check("liaison de canal falsifiee (MITM) -> refus",
          mitm.get("auth_ok") is False)


def test_identity():
    print("\n6. Identite de machine")
    pub = os.urandom(32)
    check("identifiant stable pour une meme cle",
          identity.machine_id(pub) == identity.machine_id(pub))
    check("identifiant different pour une autre cle",
          identity.machine_id(pub) != identity.machine_id(os.urandom(32)))
    check("format 3-3-3", len(identity.machine_id(pub).split()) == 3)
    check("empreinte plus longue que l identifiant",
          len(identity.fingerprint(pub)) > 20)


def test_password_kdf():
    print("\n7. Derivation du mot de passe")
    salt = os.urandom(16)
    # parametres reduits : on teste la logique, pas le cout
    k = auth.derive_password_key("secret-de-test", salt, n=1 << 12, r=8, p=1)
    k2 = auth.derive_password_key("secret-de-test", salt, n=1 << 12, r=8, p=1)
    check("deterministe", k == k2)
    check("sel different -> cle differente",
          k != auth.derive_password_key("secret-de-test", os.urandom(16),
                                        n=1 << 12, r=8, p=1))
    challenge, binding = os.urandom(32), os.urandom(32)
    proof = auth.compute_proof(k, challenge, binding)
    check("preuve valide acceptee",
          auth.verify_proof(k, challenge, binding, proof))
    check("preuve rejetee si la liaison change",
          not auth.verify_proof(k, challenge, os.urandom(32), proof))


def test_relay():
    """Chaine complete : antenne + hote + client, tout sur 127.0.0.1.

    C'est le test qui montre le point cle de l'architecture : le relais
    recopie des octets qu'il ne comprend pas, et la poignee de main
    rdlab se deroule au-dessus SANS AUCUNE MODIFICATION."""
    print("\n8. Antenne (rendez-vous + relais)")
    registry = os.path.join(os.environ["RDLAB_DATA"], "test_registry.json")
    if os.path.exists(registry):
        os.remove(registry)
    server = rendezvous.RelayServer(bind="127.0.0.1", port=0, quiet=True,
                                    registry_path=registry)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(100):
        if server.bound_port:
            break
        time.sleep(0.05)
    check("l antenne demarre", bool(server.bound_port))
    port = server.bound_port

    device = identity.DeviceIdentity.load_or_create()
    creds = auth.CredentialStore()
    if not creds.has_password:
        creds.set_password(PASSWORD)

    agent = rendezvous.RelayAgent("127.0.0.1", port, device)
    check("l hote s enregistre (signature Ed25519 verifiee)", agent.register())
    check("l antenne connait l identifiant",
          rendezvous.normalize_id(device.machine_id) in server.registrations)

    result = {}

    def _host():
        try:
            sock, _peer = agent.accept()
            ch, info, sas, binding = handshake.host_handshake(sock, device)
            result["host_sas"] = sas
            result["auth_ok"] = handshake.host_authenticate(ch, binding, creds)
            ch.send(protocol.SESSION_ACCEPTED, {"allow_input": False,
                                                "width": 800, "height": 600})
            ch.close()
        except Exception as exc:
            result["host_error"] = repr(exc)

    t = threading.Thread(target=_host, daemon=True)
    t.start()
    time.sleep(0.3)

    try:
        sock = rendezvous.relay_connect("127.0.0.1", port, device.machine_id)
        ch, core, sas, binding = handshake.client_handshake(
            sock, "client-relais", lambda c, s, x: True)
        result["client_sas"] = sas
        handshake.client_authenticate(ch, binding, lambda: PASSWORD)
        msg_type, payload = ch.recv()
        result["accepted"] = (msg_type == protocol.SESSION_ACCEPTED)
        ch.close()
    except Exception as exc:
        result["client_error"] = repr(exc)
    t.join(timeout=25)

    check("appariement par identifiant, sans IP ni port ouvert",
          "host_error" not in result and "client_error" not in result)
    check("poignee de main chiffree AU TRAVERS du relais",
          result.get("host_sas") and result["host_sas"] == result.get("client_sas"))
    check("authentification par mot de passe a travers le relais",
          result.get("auth_ok") is True)
    check("session acceptee de bout en bout", result.get("accepted") is True)

    # identifiant inconnu -> refus net, pas de fuite d'information
    try:
        rendezvous.relay_connect("127.0.0.1", port, "000 000 000", timeout=5)
        check("identifiant inconnu -> refus", False)
    except RuntimeError as exc:
        check("identifiant inconnu -> refus", "host_not_registered" in str(exc))

    # squattage d'identifiant : cle qui ne correspond pas a l'identifiant
    class _Fake:
        machine_id = device.machine_id           # identifiant vole
        public_bytes = os.urandom(32)            # mais une autre cle
        def sign(self, data):
            return b"\x00" * 64
    try:
        rendezvous.RelayAgent("127.0.0.1", port, _Fake()).register()
        check("squattage d identifiant -> refus", False)
    except RuntimeError as exc:
        check("squattage d identifiant -> refus",
              "id_does_not_match_key" in str(exc))

    server.stop()
    agent.close()


def test_keyframe_policy():
    """Regression : un ecran fige ne doit RIEN couter en bande passante.

    Le compteur d'images cles doit mesurer des images EMISES, pas des
    sondages. Sinon un bureau immobile declenche une image cle complete
    (940 Kio sur un 2560x1440) toutes les keyframe_interval boucles."""
    print("\n9. Politique d'images cles")
    from rdlab import capture
    if not capture.HAVE_CAPTURE:
        print("  (ignore : numpy/pillow absents)")
        return
    import numpy as np

    # On court-circuite mss : pas besoin d'un vrai ecran pour ce test.
    src = capture.ScreenSource.__new__(capture.ScreenSource)
    src.width, src.height, src.tile = 256, 128, 64
    src.cols, src.rows = 4, 2
    src._hashes, src._seq, src._since_keyframe = None, 0, 0
    src.keyframe_interval = 3
    canvas = np.zeros((128, 256, 3), dtype=np.uint8)
    src.grab = lambda: canvas

    first = src.next_frame(quality=70)
    check("premiere image = image cle complete",
          first is not None and first[1] == 8
          and capture.parse_frame(first[0])[2] is True)

    idle = [src.next_frame(quality=70) for _ in range(12)]
    check("12 sondages sur un ecran fige -> 0 octet emis",
          all(r is None for r in idle))
    check("le compteur d images cles n a pas bouge",
          src._since_keyframe == 0)

    canvas[0:64, 0:64] = 255          # une seule tuile change
    delta = src.next_frame(quality=70)
    check("un changement local -> 1 tuile, pas une image cle",
          delta is not None and delta[1] == 1
          and capture.parse_frame(delta[0])[2] is False)

    # apres keyframe_interval images EMISES, une image cle resorbe la derive.
    # L'image 'delta' ci-dessus compte deja pour 1 : il en reste 2 avant
    # que le compteur atteigne keyframe_interval = 3.
    for i in range(2):
        canvas[64:128, i * 64:(i + 1) * 64] = i + 1
        emitted = src.next_frame(quality=70)
        check("image emise %d : pas encore une image cle" % (i + 1),
              emitted is not None
              and capture.parse_frame(emitted[0])[2] is False)
    canvas[0:64, 64:128] = 7
    forced = src.next_frame(quality=70)
    check("image cle periodique apres N images emises",
          forced is not None and capture.parse_frame(forced[0])[2] is True
          and forced[1] == 8)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubHooks(rdhost.HostHooks):
    """Remplace l'interface graphique : consentement scripte."""

    def __init__(self, grant):
        self.grant = grant
        self.events = []

    def event(self, kind, **fields):
        self.events.append(kind)

    def consent(self, request):
        self.sas = request["sas"]
        return self.grant


def _run_service(grant, password, port):
    """Demarre HostService (le moteur de l'application) et s'y connecte."""
    device = identity.DeviceIdentity.load_or_create()
    creds = auth.CredentialStore()
    if not creds.has_password:
        creds.set_password(PASSWORD)
    creds.disable_unattended()          # on veut tester le consentement
    hooks = _StubHooks(grant)
    options = rdhost.HostOptions(bind="127.0.0.1", port=port, view_only=True)
    service = rdhost.HostService(device, creds, options, hooks)
    t = threading.Thread(target=service.start, daemon=True)
    t.start()
    for _ in range(100):
        if "listening" in hooks.events:
            break
        time.sleep(0.05)
    out = {"hooks": hooks, "service": service}
    try:
        session = rdclient.connect(
            "127.0.0.1", port=port,
            verify_cb=lambda core, status, sas: True,
            password_cb=lambda: password)
        out["session"] = session
    except rdclient.SessionRejected as exc:
        out["rejected"] = str(exc)
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def test_host_service():
    """Le chemin exact emprunte par rdlab.app : HostService + client.connect."""
    print(chr(10) + "10. Moteur de application (HostService)")
    port = _free_port()
    out = _run_service(grant=True, password=PASSWORD, port=port)
    check("le service ecoute", "listening" in out["hooks"].events)
    check("pas d erreur", "error" not in out)
    session = out.get("session")
    check("session acceptee apres consentement", session is not None)
    if session:
        check("permissions transmises au client",
              session.permissions.get("allow_input") is False
              and session.permissions.get("width", 0) > 0)
        check("le SAS vu par l interface est celui de la session",
              out["hooks"].sas == session.sas)
        if capture.HAVE_CAPTURE:
            got = False
            deadline = time.time() + 15
            while time.time() < deadline and not got:
                msg_type, payload = session.channel.recv()
                if msg_type == protocol.SCREEN_FRAME:
                    seq, q, kf, tiles = capture.parse_frame(payload)
                    got = kf and len(tiles) > 0
            check("une vraie image d ecran traverse le moteur", got)
        session.channel.close()
    out["service"].stop()
    time.sleep(0.3)

    # consentement refuse -> le client doit recevoir un refus explicite
    port = _free_port()
    out = _run_service(grant=False, password=PASSWORD, port=port)
    check("consentement refuse -> SessionRejected",
          out.get("rejected") == "consent_denied")
    out["service"].stop()
    time.sleep(0.3)

    # mauvais mot de passe -> refus avant meme de demander le consentement
    port = _free_port()
    out = _run_service(grant=True, password="mauvais-mot-de-passe", port=port)
    check("mauvais mot de passe -> refus avant le consentement",
          out.get("rejected") == "bad_password"
          and "auth_failed" in out["hooks"].events)
    out["service"].stop()


def main():
    print("=" * 58)
    print("rdlab - verification hors ligne")
    print("donnees de test : %s" % os.environ["RDLAB_DATA"])
    print("=" * 58)
    test_framing()
    test_secure_channel()
    test_key_derivation()
    test_identity()
    test_password_kdf()
    test_handshake()
    test_relay()
    test_keyframe_policy()
    test_host_service()
    print("\n%d verifications reussies, %d echecs" % (_ok, _ko))
    return 1 if _ko else 0


if __name__ == "__main__":
    sys.exit(main())
