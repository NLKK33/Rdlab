"""Section 2 : identite de la machine.

Distinction fondamentale, valable pour tout systeme :

  IDENTIFIANT  = "qui pretends-tu etre ?"   -> public, non secret
  AUTHENTIFICATION = "prouve-le"            -> secret / preuve cryptographique
  AUTORISATION = "que peux-tu faire ?"      -> politique, decidee apres coup

L'identifiant de machine est ici derive d'une cle Ed25519 long terme
generee a la premiere execution. Deux proprietes :

  - il est stable (il survit a un changement d'IP, de reseau, de FAI) ;
  - il est *verifiable* : seul le detenteur de la cle privee peut signer
    un defi. Un identifiant seul ne prouve rien, il se copie.

Pourquoi pas l'adresse IP comme identifiant public ?
  - elle change (DHCP, 4G, itinerance) ;
  - derriere un NAT, elle est partagee par des dizaines de machines ;
  - elle localise l'utilisateur (donnee personnelle) ;
  - elle ne se prouve pas : elle s'usurpe.

Le fichier de cle est stocke en clair dans un repertoire visible du
projet. Un produit reel le protegerait via le trousseau du systeme
(DPAPI / Keychain / kernel keyring) ou une puce TPM.
"""

import base64
import hashlib
import json
import os
import stat

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

DATA_DIR = os.environ.get("RDLAB_DATA", os.path.join(os.getcwd(), "rdlab-data"))
KEY_FILE = "device_key.json"


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def machine_id(public_bytes):
    """9 chiffres groupes, derives de la cle publique.

    C'est un raccourci ergonomique (dictable au telephone), PAS un
    secret et PAS une preuve. 9 chiffres = ~30 bits : un attaquant peut
    chercher une collision. C'est pourquoi l'empreinte complete
    (fingerprint) reste la seule chose a verifier serieusement.
    """
    digest = hashlib.sha256(public_bytes).digest()
    n = int.from_bytes(digest[:8], "big") % 1_000_000_000
    s = "%09d" % n
    return "%s %s %s" % (s[0:3], s[3:6], s[6:9])


def fingerprint(public_bytes):
    """Empreinte complete, base32, groupee par 4. C'est l'identite reelle."""
    digest = hashlib.sha256(public_bytes).digest()
    b32 = base64.b32encode(digest[:20]).decode("ascii")
    return "-".join(b32[i:i + 4] for i in range(0, len(b32), 4))


class DeviceIdentity:
    """Cle long terme de l'appareil + son identifiant derive."""

    def __init__(self, private_key):
        self._priv = private_key
        self.public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        self.machine_id = machine_id(self.public_bytes)
        self.fingerprint = fingerprint(self.public_bytes)

    @classmethod
    def load_or_create(cls, path=None):
        path = path or os.path.join(_ensure_dir(), KEY_FILE)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = base64.b64decode(json.load(fh)["private_key"])
            return cls(Ed25519PrivateKey.from_private_bytes(raw))

        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption())
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"private_key": base64.b64encode(raw).decode()}, fh)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)   # 0600 (POSIX)
        except OSError:
            pass
        return cls(priv)

    def sign(self, data):
        return self._priv.sign(data)


def verify_signature(public_bytes, signature, data):
    """Leve InvalidSignature si la preuve ne colle pas."""
    Ed25519PublicKey.from_public_bytes(public_bytes).verify(signature, data)


# --- Trust On First Use, cote client -------------------------------

class KnownHosts:
    """Memorise fingerprint -> machine, comme ~/.ssh/known_hosts.

    Premiere connexion : on affiche l'empreinte, l'humain la compare avec
    celle lue sur l'ecran de l'hote. Connexions suivantes : toute
    divergence est une alerte (rotation de cle legitime... ou MITM).
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(_ensure_dir(), "known_hosts.json")
        self._entries = {}
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self._entries = json.load(fh)

    def check(self, mid, fp):
        """-> 'new' | 'match' | 'mismatch'"""
        known = self._entries.get(mid)
        if known is None:
            return "new"
        return "match" if known == fp else "mismatch"

    def remember(self, mid, fp):
        self._entries[mid] = fp
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._entries, fh, indent=2)

    def forget(self, mid):
        """Revocation cote client : on oublie un appareil."""
        self._entries.pop(mid, None)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._entries, fh, indent=2)
