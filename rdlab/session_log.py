"""Section 7/8 : journalisation des connexions.

Un journal n'empeche rien : il rend les choses *constatables*. C'est la
contrepartie indispensable de l'acces sans surveillance. Il doit etre :
  - en append seul, jamais reecrit par le code de session ;
  - lisible par l'utilisateur sans outil special (JSONL) ;
  - exhaustif sur les tentatives *echouees* aussi, pas seulement
    les reussites (c'est la que se voient les attaques).

Un produit reel y ajouterait : rotation, horodatage signe ou chainage
par hash (pour detecter l'effacement), et export vers un SIEM.
"""

import json
import os
import time

from . import identity

LOG_FILE = "sessions.log"


def _path():
    return os.path.join(identity._ensure_dir(), LOG_FILE)


def log(event, **fields):
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
    record.update(fields)
    with open(_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def tail(n=20):
    p = _path()
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8") as fh:
        lines = fh.readlines()[-n:]
    return [json.loads(l) for l in lines]
