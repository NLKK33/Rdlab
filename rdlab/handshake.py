"""La poignee de main, partagee par l'hote et le client.

           CLIENT                              HOTE
             |                                   |
             |------------- HELLO -------------->|   clair
             |   version, nom, cle eph. X25519   |
             |                                   |
             |<----------- HELLO_ACK ------------|   clair
             |   cle eph. X25519, machine_id,    |
             |   cle publique Ed25519 long terme,|
             |   SIGNATURE du transcript         |
             |                                   |
        [ les deux calculent le meme secret partage X25519,      ]
        [ derivent 4 cles via HKDF, lie au hash du transcript    ]
             |                                   |
             |=== a partir d'ici : tout est chiffre (AES-GCM) ===|
             |                                   |
             |<--------- AUTH_REQUEST -----------|
             |   sel scrypt, parametres, defi    |
             |---------- AUTH_RESPONSE --------->|
             |   HMAC(scrypt(pw), defi || liaison)|
             |                                   |
             |<------- SESSION_ACCEPTED ---------|

Deux verrous anti-MITM, independants :

  1. SIGNATURE : l'hote signe le transcript avec sa cle long terme. Un
     attaquant au milieu ne possede pas cette cle ; il ne peut donc pas
     produire une signature valide sur SON transcript. Le client verifie
     que l'empreinte correspond a ce qu'il connait (TOFU, known_hosts).
     C'est l'equivalent du certificat serveur en TLS.

  2. SAS (6 chiffres) : filet de securite pour la toute premiere
     connexion, quand le client ne connait pas encore l'empreinte. Les
     deux humains comparent le code hors bande.

Et la liaison de canal dans AUTH_RESPONSE empeche un relais de la preuve
de mot de passe vers le vrai hote.
"""

import base64
import json

from cryptography.exceptions import InvalidSignature

from . import auth, crypto, identity, protocol

VERSION = 1


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    return base64.b64decode(s)


class HandshakeError(Exception):
    pass


# --------------------------------------------------------------- hote
def host_handshake(sock, device):
    """Cote hote. Retourne (canal_chiffre, infos_client, sas, liaison)."""
    msg_type, hello_bytes = protocol.read_frame_plain(sock)
    if msg_type != protocol.HELLO:
        raise HandshakeError("attendu HELLO, recu %s" % protocol.name(msg_type))
    hello = json.loads(hello_bytes.decode("utf-8"))
    if hello.get("version") != VERSION:
        raise HandshakeError("version de protocole incompatible")

    eph = crypto.EphemeralKeypair()
    core = {
        "version": VERSION,
        "eph_pub": _b64(eph.public_bytes),
        "machine_id": device.machine_id,
        "fingerprint": device.fingerprint,
        "device_pub": _b64(device.public_bytes),
    }
    core_bytes = json.dumps(core, separators=(",", ":"), sort_keys=True).encode()

    th = crypto.transcript_hash(hello_bytes, core_bytes)
    signature = device.sign(th)
    protocol.write_frame_plain(sock, protocol.HELLO_ACK, {
        "core": _b64(core_bytes), "signature": _b64(signature)})

    shared = eph.exchange(_unb64(hello["eph_pub"]))
    keys = crypto.derive_keys(shared, th)
    channel = crypto.SecureChannel(sock, keys["host_to_client"],
                                   keys["client_to_host"])
    client_info = {"name": str(hello.get("name", "?"))[:64],
                   "version": hello.get("version")}
    return (channel, client_info, crypto.short_auth_string(keys["sas"]),
            keys["auth_binding"])


def host_authenticate(channel, binding, creds):
    """Envoie le defi, verifie la preuve. Retourne True/False."""
    challenge = crypto.random_bytes(32)
    channel.send(protocol.AUTH_REQUEST, {
        "salt": _b64(creds.salt),
        "n": auth.SCRYPT_N, "r": auth.SCRYPT_R, "p": auth.SCRYPT_P,
        "challenge": _b64(challenge),
    })
    msg_type, payload = channel.recv()
    if msg_type != protocol.AUTH_RESPONSE:
        raise HandshakeError("attendu AUTH_RESPONSE")
    proof = _unb64(protocol.as_json(payload)["proof"])
    return auth.verify_proof(creds.password_key, challenge, binding, proof)


# ------------------------------------------------------------- client
def client_handshake(sock, client_name, on_verify):
    """Cote client.

    `on_verify(info, statut_tofu, sas)` doit retourner True pour
    continuer. C'est la DECISION HUMAINE : on ne l'automatise pas."""
    eph = crypto.EphemeralKeypair()
    hello = {"version": VERSION, "name": client_name,
             "eph_pub": _b64(eph.public_bytes)}
    hello_bytes = json.dumps(hello, separators=(",", ":")).encode()
    protocol.write_frame_plain(sock, protocol.HELLO, hello_bytes)

    msg_type, payload = protocol.read_frame_plain(sock)
    if msg_type != protocol.HELLO_ACK:
        raise HandshakeError("attendu HELLO_ACK, recu %s"
                             % protocol.name(msg_type))
    envelope = json.loads(payload.decode("utf-8"))
    core_bytes = _unb64(envelope["core"])
    core = json.loads(core_bytes.decode("utf-8"))

    th = crypto.transcript_hash(hello_bytes, core_bytes)
    device_pub = _unb64(core["device_pub"])
    try:
        identity.verify_signature(device_pub, _unb64(envelope["signature"]), th)
    except InvalidSignature:
        raise HandshakeError(
            "signature de l'hote invalide - connexion interceptee ?")

    # L'empreinte annoncee doit correspondre a la cle reellement fournie,
    # sinon un hote pourrait annoncer l'empreinte de quelqu'un d'autre.
    if core["fingerprint"] != identity.fingerprint(device_pub):
        raise HandshakeError("empreinte incoherente avec la cle publique")
    if core["machine_id"] != identity.machine_id(device_pub):
        raise HandshakeError("identifiant incoherent avec la cle publique")

    shared = eph.exchange(_unb64(core["eph_pub"]))
    keys = crypto.derive_keys(shared, th)
    sas = crypto.short_auth_string(keys["sas"])

    known = identity.KnownHosts()
    status = known.check(core["machine_id"], core["fingerprint"])
    if not on_verify(core, status, sas):
        raise HandshakeError("verification refusee par l'utilisateur")
    if status != "match":
        known.remember(core["machine_id"], core["fingerprint"])

    channel = crypto.SecureChannel(sock, keys["client_to_host"],
                                   keys["host_to_client"])
    return channel, core, sas, keys["auth_binding"]


def client_authenticate(channel, binding, ask_password):
    """Repond au defi. `ask_password()` demande le secret a l'humain."""
    msg_type, payload = channel.recv()
    if msg_type != protocol.AUTH_REQUEST:
        raise HandshakeError("attendu AUTH_REQUEST, recu %s"
                             % protocol.name(msg_type))
    req = protocol.as_json(payload)
    password = ask_password()
    key = auth.derive_password_key(password, _unb64(req["salt"]),
                                   req["n"], req["r"], req["p"])
    proof = auth.compute_proof(key, _unb64(req["challenge"]), binding)
    channel.send(protocol.AUTH_RESPONSE, {"proof": _b64(proof)})
