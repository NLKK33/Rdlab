"""Reglages integres a la construction.

L'adresse de l'antenne ne change jamais pour un utilisateur donne : la
saisir a chaque installation est une corvee inutile et une source
d'erreur. Elle est donc integree ici, et l'application s'en sert
automatiquement.

Ordre de priorite, du plus fort au plus faible :

  1. la variable d'environnement RDLAB_RELAY, pour essayer une autre
     antenne sans rien reconstruire ;
  2. ce que l'utilisateur a saisi dans les options avancees (memorise
     dans settings.json) ;
  3. DEFAULT_RELAY ci-dessous.

POUR CHANGER D'ANTENNE : modifiez DEFAULT_RELAY et reconstruisez
l'executable (build-exe.bat), ou saisissez simplement la nouvelle
adresse dans les options avancees de l'application.

ATTENTION SI VOUS RENDEZ CE DEPOT PUBLIC : cette ligne publie l'adresse
de votre serveur. Ce n'est pas un secret -- un port ouvert se trouve de
toute facon par balayage -- mais autant le savoir. Pour l'eviter,
laissez DEFAULT_RELAY vide et passez par RDLAB_RELAY.
"""

import os

DEFAULT_RELAY = "195.200.15.62:7800"


def default_relay():
    return os.environ.get("RDLAB_RELAY", DEFAULT_RELAY).strip()
