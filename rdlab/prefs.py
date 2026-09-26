"""Reglages memorises entre deux lancements.

Sans cela, l'utilisateur retape l'adresse de son antenne a chaque
ouverture de l'application. C'est la friction n.1 d'un outil comme
celui-ci : l'adresse ne change jamais, mais il faut la ressaisir.

Ce fichier ne contient AUCUN secret : ni mot de passe, ni cle. Ces
elements vivent dans credentials.json et device_key.json, qui ont leurs
propres regles. Les reglages ici sont de simples preferences
d'affichage et de connexion, et une corruption du fichier ne doit
jamais empecher l'application de demarrer.
"""

import json
import os

from . import identity

FILE = "settings.json"

DEFAULTS = {
    "relay": "",             # adresse de l'antenne, cote partage
    "share_mode": "relay",   # "relay" ou "direct"
    "connect_relay": "",     # adresse de l'antenne, cote client
    "connect_mode": "relay",
    "connect_port": "7700",
    "lan_port": "7700",
    "quality": "balanced",
    "view_only": False,
    "last_target": "",       # dernier identifiant ou IP joint
}


class Prefs:
    def __init__(self, path=None):
        self.path = path or os.path.join(identity._ensure_dir(), FILE)
        self.data = dict(DEFAULTS)
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                # on ne reprend que les cles connues : un fichier d'une
                # version ulterieure ne doit pas injecter n'importe quoi
                for k in DEFAULTS:
                    if k in stored:
                        self.data[k] = stored[k]
        except (OSError, ValueError):
            pass     # fichier absent ou illisible : on garde les defauts

    def get(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        if self.data.get(key) == value:
            return
        self.data[key] = value
        self.save()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
        except OSError:
            pass     # un reglage non sauvegarde ne doit rien casser
