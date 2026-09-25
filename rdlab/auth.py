"""Etape 2 : authentification de l'utilisateur (mot de passe).

Principe : le mot de passe ne traverse JAMAIS le reseau, meme chiffre.
On fait une preuve de connaissance (challenge-response) :

    hote   : defi aleatoire R + sel + parametres scrypt
    client : k  = scrypt(mot_de_passe, sel)
             preuve = HMAC-SHA256(k, R || liaison_de_canal)
    hote   : recalcule et compare en temps constant

Deux proprietes importantes :

  1. "liaison de canal" (channel binding) : la preuve inclut un secret
     derive de la session TLS-like courante. Un homme du milieu qui
     relaie la preuve vers le vrai hote echoue, car sa session avec le
     vrai hote a une liaison differente. Sans ca, le challenge-response
     est vulnerable au relais.

  2. scrypt (memory-hard) rend une attaque par dictionnaire couteuse si
     le fichier de verification fuit.

SIMPLIFICATION PEDAGOGIQUE ASSUMEE : l'hote stocke k = scrypt(pw, sel).
Qui lit ce fichier peut se faire passer pour un client legitime (c'est
un "verifieur symetrique"). Les systemes serieux utilisent un PAKE
augmente (SPAKE2+, OPAQUE) ou le serveur stocke une valeur qui permet de
verifier sans permettre d'usurper, et ou aucune attaque par dictionnaire
hors ligne n'est possible meme en ecoutant le reseau.
"""

import base64
import hmac
import json
import os
import time

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import identity

SCRYPT_N = 1 << 15   # 32768 : ~100-200 ms, cout memoire ~32 Mio
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32
CRED_FILE = "credentials.json"


def derive_password_key(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


def compute_proof(password_key, challenge, channel_binding):
    """La preuve envoyee par le client."""
    mac = hmac.new(password_key, digestmod="sha256")
    mac.update(challenge)
    mac.update(channel_binding)
    return mac.digest()


def verify_proof(password_key, challenge, channel_binding, proof):
    expected = compute_proof(password_key, challenge, channel_binding)
    return hmac.compare_digest(expected, proof)   # temps constant


class CredentialStore:
    """Les reglages d'acces de la machine hote. Fichier volontairement
    lisible : l'utilisateur doit pouvoir auditer ce qu'il a active."""

    DEFAULTS = {
        "unattended_enabled": False,   # acces sans surveillance : OFF par defaut
        "salt": None,
        "password_key": None,
        "created_at": None,
        "allow_input": True,           # autorisation : controle clavier/souris
        "allow_clipboard": False,
        "max_sessions": 1,
    }

    def __init__(self, path=None):
        self.path = path or os.path.join(identity._ensure_dir(), CRED_FILE)
        self.data = dict(self.DEFAULTS)
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data.update(json.load(fh))

    def save(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)

    # -- mot de passe -----------------------------------------------
    def set_password(self, password):
        if len(password) < 10:
            raise ValueError("mot de passe trop court (10 caracteres minimum)")
        salt = os.urandom(16)
        key = derive_password_key(password, salt)
        self.data["salt"] = base64.b64encode(salt).decode()
        self.data["password_key"] = base64.b64encode(key).decode()
        self.data["created_at"] = time.time()
        self.save()

    def clear_password(self):
        self.data["salt"] = None
        self.data["password_key"] = None
        self.data["unattended_enabled"] = False
        self.save()

    @property
    def has_password(self):
        return bool(self.data.get("password_key"))

    @property
    def salt(self):
        return base64.b64decode(self.data["salt"])

    @property
    def password_key(self):
        return base64.b64decode(self.data["password_key"])

    # -- acces sans surveillance ------------------------------------
    def enable_unattended(self):
        """Section 8 : activation EXPLICITE, impossible sans mot de passe."""
        if not self.has_password:
            raise ValueError(
                "definissez d'abord un mot de passe (--set-password)")
        self.data["unattended_enabled"] = True
        self.save()

    def disable_unattended(self):
        """Le coupe-circuit. Doit toujours exister et etre trivial a utiliser."""
        self.data["unattended_enabled"] = False
        self.save()

    @property
    def unattended(self):
        return bool(self.data.get("unattended_enabled")) and self.has_password
