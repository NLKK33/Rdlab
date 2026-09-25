"""Etape 6 : chiffrement de bout en bout du canal.

Schema : X25519 (accord de cle ephemere) -> HKDF-SHA256 (derivation) ->
AES-256-GCM (chiffrement authentifie), avec des cles differentes par
sens de communication.

Pourquoi ephemere ? Si la cle long terme de l'hote fuit demain, un
enregistrement du trafic d'aujourd'hui reste indechiffrable : c'est la
"forward secrecy". Les cles ephemeres ne touchent jamais le disque.

Le nonce GCM ne doit JAMAIS etre reutilise avec la meme cle : on utilise
un compteur strictement croissant, distinct par sens. Ca donne aussi
gratuitement la protection anti-rejeu et anti-reordonnancement : une
trame rejouee arrive avec un compteur deja consomme et echoue.
"""

import os
import struct
import threading

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import protocol

KEY_LEN = 32
NONCE_LEN = 12
_LEN = struct.Struct("!I")


class EphemeralKeypair:
    """Cle X25519 jetable, une par connexion."""

    def __init__(self):
        self._priv = X25519PrivateKey.generate()

    @property
    def public_bytes(self):
        from cryptography.hazmat.primitives import serialization
        return self._priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)

    def exchange(self, peer_public_bytes):
        peer = X25519PublicKey.from_public_bytes(peer_public_bytes)
        return self._priv.exchange(peer)


def derive_keys(shared_secret, transcript_hash):
    """Un secret partage -> trois secrets a roles distincts.

    On lie la derivation au 'transcript' (hash de tout ce qui a ete
    echange en clair pendant la poignee de main). Un attaquant qui
    modifie un octet du HELLO obtient des cles differentes de celles du
    pair : la session casse immediatement au lieu de reussir en mode
    degrade. C'est le principe du "channel binding".
    """
    def _hkdf(info, length=KEY_LEN):
        return HKDF(algorithm=hashes.SHA256(), length=length,
                    salt=transcript_hash, info=info).derive(shared_secret)

    return {
        "host_to_client": _hkdf(b"rdlab h2c"),
        "client_to_host": _hkdf(b"rdlab c2h"),
        "auth_binding": _hkdf(b"rdlab auth binding"),
        "sas": _hkdf(b"rdlab sas", 8),
    }


def short_auth_string(sas_secret):
    """6 chiffres derives du secret de session, a comparer de vive voix.

    Contre-mesure au MITM quand aucune cle de l'hote n'est connue
    d'avance : un attaquant au milieu negocie deux sessions distinctes,
    donc deux SAS differents. Les deux humains les comparent hors bande
    (telephone, presence physique). Meme idee que la verification de
    l'empreinte SSH ou du code d'appariement Bluetooth.
    """
    n = int.from_bytes(sas_secret[:4], "big") % 1_000_000
    return "%06d" % n


def transcript_hash(*chunks):
    digest = hashes.Hash(hashes.SHA256())
    for c in chunks:
        digest.update(_LEN.pack(len(c)))   # longueur prefixee : pas d'ambiguite
        digest.update(c)
    return digest.finalize()


class SecureChannel:
    """Enveloppe une socket TCP : chaque trame applicative est chiffree.

    Format sur le fil, apres la poignee de main :

        +----------------+--------------------------------------+
        | longueur 4 o.  | AES-GCM( type || payload ) + tag 16 o.|
        +----------------+--------------------------------------+

    Le type de message est DANS le chiffre : un observateur ne sait meme
    pas distinguer un mouvement de souris d'un PING.
    """

    def __init__(self, sock, send_key, recv_key):
        self._sock = sock
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()

    @staticmethod
    def _nonce(counter):
        return b"\x00" * 4 + struct.pack("!Q", counter)

    def send(self, msg_type, payload=b""):
        frame = protocol.encode(msg_type, payload)
        with self._send_lock:
            nonce = self._nonce(self._send_ctr)
            self._send_ctr += 1
            blob = self._send.encrypt(nonce, frame, None)
            self._sock.sendall(_LEN.pack(len(blob)) + blob)

    def recv(self):
        with self._recv_lock:
            (size,) = _LEN.unpack(protocol.recv_exact(self._sock, 4))
            if size > protocol.MAX_PAYLOAD + 64:
                raise ValueError("trame chiffree aberrante")
            blob = protocol.recv_exact(self._sock, size)
            nonce = self._nonce(self._recv_ctr)
            self._recv_ctr += 1
        # Si le dechiffrement echoue : trame alteree, rejouee, ou hors
        # ordre. Il n'y a pas de "degradation gracieuse" possible.
        frame = self._recv.decrypt(nonce, blob, None)
        return protocol.decode(frame)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def random_bytes(n=32):
    return os.urandom(n)
