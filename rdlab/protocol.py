"""Etape 1 : cadrage des messages (couche application).

Un flux TCP est un tuyau d'octets sans frontieres. Il faut donc definir
nous-memes ou commence et ou finit un message : c'est le "framing".

Trame en clair (avant chiffrement) :

    +--------+-----------------+----------------------+
    | type   | longueur        | payload              |
    | 1 o.   | 4 o. (big-end.) | 0..MAX_PAYLOAD o.    |
    +--------+-----------------+----------------------+

Le payload est soit du JSON (messages de controle : lisibles, faciles a
deboguer), soit du binaire brut (images : compact). Un systeme
professionnel utiliserait un encodage binaire partout (protobuf,
CBOR, ou un format maison) pour eviter le cout du JSON.
"""

import json
import struct

MAGIC = b"RDL1"          # identifie le protocole + sa version majeure
MAX_PAYLOAD = 8 << 20    # 8 Mio : garde-fou anti "longueur delirante"

# --- Poignee de main / session -------------------------------------
HELLO = 0x01             # client -> hote : "voici qui je suis, ma cle ephemere"
HELLO_ACK = 0x02         # hote -> client : cle ephemere + identite signee
AUTH_REQUEST = 0x10      # hote -> client : defi + parametres de derivation
AUTH_RESPONSE = 0x11     # client -> hote : preuve de connaissance du mot de passe
CONSENT_PENDING = 0x14   # hote -> client : "j'attends le OK de l'humain"
SESSION_ACCEPTED = 0x12  # hote -> client : session ouverte (+ permissions)
SESSION_REJECTED = 0x13  # hote -> client : refus (+ motif)

# --- Flux ecran ----------------------------------------------------
SCREEN_INFO = 0x20       # hote -> client : geometrie de l'ecran partage
SCREEN_FRAME = 0x21      # hote -> client : une image (complete ou par tuiles)
FRAME_ACK = 0x22         # client -> hote : "frame N affichee" (controle de debit)

# --- Entrees -------------------------------------------------------
MOUSE_EVENT = 0x30       # client -> hote : deplacement / clic / molette
KEY_EVENT = 0x31         # client -> hote : touche enfoncee / relachee

# --- Service -------------------------------------------------------
PING = 0x40              # mesure du RTT, garde la session vivante
PONG = 0x41
QUALITY_HINT = 0x42      # client -> hote : "ralentis" / "tu peux monter"
DISCONNECT = 0x50        # fin propre, avec motif

NAMES = {}
for _k, _v in list(globals().items()):
    if _k.isupper() and isinstance(_v, int) and _k != "MAX_PAYLOAD":
        NAMES[_v] = _k


def name(msg_type):
    return NAMES.get(msg_type, "0x%02X" % msg_type)


_HDR = struct.Struct("!BI")
HEADER_SIZE = _HDR.size


def encode(msg_type, payload=b""):
    """Serialise un message en une trame complete."""
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    elif isinstance(payload, str):
        payload = payload.encode("utf-8")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload trop gros: %d" % len(payload))
    return _HDR.pack(msg_type, len(payload)) + payload


def decode(frame):
    """Inverse de encode() sur une trame deja complete."""
    msg_type, length = _HDR.unpack_from(frame, 0)
    body = frame[HEADER_SIZE:HEADER_SIZE + length]
    if len(body) != length:
        raise ValueError("trame tronquee")
    return msg_type, body


def as_json(payload):
    return json.loads(payload.decode("utf-8"))


# --- Lecture/ecriture EN CLAIR : seulement pendant la poignee de main.
# Des que les cles de session existent, tout passe par crypto.SecureChannel.

def recv_exact(sock, n):
    """recv() peut rendre moins d'octets que demande : on boucle."""
    chunks = []
    got = 0
    while got < n:
        part = sock.recv(min(65536, n - got))
        if not part:
            raise ConnectionError("pair deconnecte")
        chunks.append(part)
        got += len(part)
    return b"".join(chunks)


def read_frame_plain(sock):
    head = recv_exact(sock, HEADER_SIZE)
    msg_type, length = _HDR.unpack(head)
    if length > MAX_PAYLOAD:
        raise ValueError("longueur annoncee aberrante: %d" % length)
    return msg_type, recv_exact(sock, length) if length else b""


def write_frame_plain(sock, msg_type, payload=b""):
    sock.sendall(encode(msg_type, payload))
