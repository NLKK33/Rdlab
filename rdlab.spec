# -*- mode: python ; coding: utf-8 -*-
"""Recette PyInstaller : rdlab.exe, un fichier unique, sans console.

Choix de conception :

  --onefile : un seul .exe a copier sur l'autre machine. Le demarrage est
    un peu plus lent (l'archive est extraite dans un dossier temporaire a
    chaque lancement) mais c'est le prix d'un logiciel qu'on transmet par
    cle USB ou par message.

  --windowed : aucune fenetre de console derriere l'interface. Revers :
    les erreurs precoces n'ont nulle part ou s'afficher, d'ou le bloc
    de secours dans rdlab_exe.py qui les montre dans une boite.

  L'executable NE CONTIENT PAS le module rendezvous cote serveur ? Si,
  il est inclus : la meme application peut depanner en lancant une
  antenne locale. Ce qui est exclu, ce sont les bibliotheques inutiles
  (matplotlib, scipy, pytest...) que PyInstaller embarquerait sinon en
  suivant les dependances optionnelles de numpy et pillow.
"""

block_cipher = None

a = Analysis(
    ['rdlab_exe.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # pynput choisit son implementation a l'execution : PyInstaller
        # ne peut pas la deviner par analyse statique.
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'scipy', 'pandas', 'pytest', 'setuptools', 'pydoc',
        'IPython', 'notebook', 'PIL.ImageQt', 'PyQt5', 'PySide2',
        'numpy.testing', 'numpy.f2py',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='rdlab',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='rdlab.ico',
)
