"""rdlab - laboratoire pedagogique de bureau a distance.

Aucun rapport avec un produit commercial : c'est une implementation
generique, minimale et volontairement lisible des mecanismes communs a
tous les outils de bureau a distance (VNC, RDP, et derives).

Regles du labo, appliquees dans le code :
  - l'hote affiche toujours ce qu'il partage (pas de furtivite) ;
  - chaque session est journalisee en clair dans rdlab-data/sessions.log ;
  - l'acces sans surveillance est desactive par defaut et doit etre
    active explicitement par l'utilisateur de la machine hote ;
  - aucun contournement de protection systeme, aucune persistance cachee.
"""

__version__ = "0.8.0"
