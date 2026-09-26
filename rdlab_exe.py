#!/usr/bin/env python3
"""Point d'entree de l'executable autonome (rdlab.exe).

Pourquoi un fichier separe de rdlab.pyw : l'executable est construit
sans console (`--windowed`). Une erreur survenant avant l'ouverture de
la fenetre n'aurait alors nulle part ou s'afficher -- l'utilisateur
double-clique, rien ne se passe, et il n'a aucun moyen de savoir
pourquoi. C'est exactement le defaut qu'avait le lanceur .bat.

On attrape donc tout ce qui remonte et on le montre dans une boite de
dialogue, avec l'emplacement du dossier de donnees pour que le rapport
soit exploitable.
"""

import os
import sys


def _show_error(text):
    """Derniere chance d'informer : une boite, sinon la sortie standard."""
    try:
        import tkinter as tk
        from tkinter import scrolledtext
        root = tk.Tk()
        root.title("rdlab - erreur au demarrage")
        root.geometry("700x420")
        root.configure(bg="#12121a")
        tk.Label(root, text="rdlab n'a pas pu demarrer", bg="#12121a",
                 fg="#f7768e", font=("Segoe UI Semibold", 13)).pack(
            padx=20, pady=(18, 8))
        box = scrolledtext.ScrolledText(root, bg="#0d0d12", fg="#e6e6f0",
                                        font=("Consolas", 9), relief="flat",
                                        wrap="word")
        box.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        box.insert("1.0", text)
        box.configure(state="disabled")
        tk.Button(root, text="Fermer", command=root.destroy, bg="#2a2a3a",
                  fg="#e6e6f0", relief="flat", padx=18, pady=7).pack(
            pady=(0, 16))
        root.mainloop()
    except Exception:
        sys.stderr.write(text + "\n")


def main():
    try:
        from rdlab import identity
        from rdlab.app import main as app_main
    except Exception:
        import traceback
        _show_error("Les composants de rdlab n'ont pas pu etre charges.\n\n"
                    + traceback.format_exc())
        return 1

    try:
        app_main()
    except Exception:
        import traceback
        _show_error(
            "Erreur pendant l'execution.\n\n"
            "Dossier de donnees : %s\n"
            "Executable        : %s\n\n%s"
            % (identity.DATA_DIR, sys.executable, traceback.format_exc()))
        return 1
    return 0


if __name__ == "__main__":
    # Lance depuis les sources (et non depuis l'exe), le dossier du projet
    # n'est pas forcement dans sys.path.
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
