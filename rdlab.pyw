#!/usr/bin/env python3
"""Point d'entree graphique. Sous Windows, l'extension .pyw lance
l'application sans fenetre de console : double-cliquez ce fichier."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rdlab.app import main      # noqa: E402

main()
