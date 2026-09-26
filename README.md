# rdlab — laboratoire pédagogique de bureau à distance

Implémentation **générique** et volontairement lisible des mécanismes communs à
tous les outils de bureau à distance. Ce n'est la reproduction d'aucun produit
commercial : ni son code, ni son protocole, ni son infrastructure. Les principes
utilisés ici (capture par régions, chiffrement de session, traversée de NAT,
relais) sont publics et documentés depuis RFB/VNC (1998) et WebRTC.

## Cadre d'utilisation

Ce logiciel n'est destiné qu'à des machines **vous appartenant** ou à un réseau
de test sur lequel vous avez une autorisation explicite. Accéder à l'ordinateur
d'autrui sans son accord est illégal dans la quasi-totalité des juridictions,
et aucune disposition de ce code ne vise à le faciliter : le consentement est
obligatoire par défaut, chaque session est journalisée, et rien n'est dissimulé
à la personne présente devant la machine partagée.

Logiciel fourni sans garantie (voir `LICENSE`). Il n'a **pas** été audité et
contient des simplifications pédagogiques assumées, listées plus bas : ne
l'utilisez pas pour protéger quoi que ce soit d'important.

## Règles du labo, appliquées dans le code

| Règle | Où elle est appliquée |
|---|---|
| Deux machines vous appartenant, ou réseau de test | vous |
| Consentement explicite à chaque connexion | `HostHooks.consent()` — défaut = refus, expiration = refus |
| Aucune furtivité | bannière rouge nommant qui vous observe, état rafraîchi toutes les 2 s |
| Aucune persistance cachée | aucun service, aucune clé de registre ; tout vit dans `rdlab-data/` |
| Aucun contournement de protection | on utilise les API d'injection publiques, l'UIPI/Wayland/Accessibilité restent souverains |
| Aucune récupération de mot de passe | le mot de passe ne traverse jamais le réseau, et n'est pas stocké en clair |
| Journalisation | `rdlab-data/sessions.log` (JSONL, append seul) |

## L'application

```bash
python -m rdlab
```

Sous Windows, double-cliquez **`lancer-rdlab.bat`** (ou `rdlab.pyw`) — pas de
console. Sous Linux/macOS : `./lancer-rdlab.sh`.

Une seule fenêtre, trois onglets :

| Onglet | Ce qu'on y fait |
|---|---|
| **Partager mon écran** | identifiant + empreinte (copiables), mot de passe, accès sans surveillance, observation seule, choix antenne ou réseau local, démarrage/arrêt, coupe-circuit de session |
| **Se connecter** | machine à joindre (identifiant ou IP), antenne, qualité, connexion |
| **Journal** | l'historique complet des connexions, coloré par gravité |

### Pourquoi une application et pas seulement des scripts

Les décisions de sécurité de ce logiciel sont prises par un **humain** : accorder
une session, vérifier une empreinte, activer l'accès sans surveillance. En ligne
de commande, ces décisions se prennent dans un terminal que personne ne regarde.
Ici ce sont des boîtes de dialogue modales, **défaut sur « refuser »**, avec un
compte à rebours visible.

La règle de non-furtivité est appliquée par l'interface elle-même : quand une
session est ouverte, la bannière passe en **rouge** et nomme qui vous observe.
On ne peut pas partager son écran sans le voir.

Le moteur reste le même qu'en ligne de commande : `HostService` et
`client.connect()` ne savent pas s'ils sont pilotés par un terminal ou par des
widgets. Ils parlent à un objet `HostHooks` / à des fonctions de rappel. Une
seule implémentation du protocole, deux présentations.

### Règle des fils d'exécution

Un seul fil touche aux widgets. Les fils de fond passent par `app._ui()`, qui
reporte l'appel sur la boucle tkinter. Les demandes qui exigent une réponse
humaine (consentement, empreinte, mot de passe) bloquent le fil de fond sur un
`threading.Event` pendant que l'interface affiche le dialogue.

## Installation

### Windows

1. Installez **Python 3.9+** depuis [python.org](https://www.python.org/downloads/)
   en cochant **« Add python.exe to PATH »** sur le premier écran.
2. Récupérez le projet (`git clone`, ou *Code → Download ZIP* sur GitHub).
3. Double-cliquez **`installer-windows.bat`**.

Il vérifie Python et tkinter, crée l'environnement isolé, installe les
dépendances et propose de lancer l'application. Ensuite, le lancement
quotidien se fait par **`lancer-rdlab.bat`**.

> **Ne copiez jamais le dossier `rdlab-data/` d'une machine à l'autre.** Il
> contient la clé privée qui *est* l'identité de la machine. Deux machines
> partageant cette clé auraient le même identifiant et la même empreinte :
> la vérification d'identité ne voudrait plus rien dire. Chaque installation
> génère la sienne au premier démarrage.

### Linux / macOS, ou installation manuelle

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows ;  source .venv/bin/activate sous Linux/macOS
pip install -r requirements.txt
```

> Sous Windows, si `pip install cryptography` échoue avec « Nom de fichier ou
> extension trop long », créez le venv à un chemin court (`C:\venvs\rdlab`).

## Vérification sans deuxième machine

```bash
python selftest.py
```

**50 vérifications** hors ligne : cadrage, chiffrement, anti-rejeu, poignée de
main, signature de l'hôte, SAS identique des deux côtés, refus d'un mauvais mot
de passe, refus d'une liaison de canal falsifiée (simulation de MITM), chaîne
complète antenne + hôte + client sur `127.0.0.1`, refus de squattage
d'identifiant, politique d'images clés (un écran figé doit coûter 0 octet), et
le moteur de l'application de bout en bout (`HostService` + `client.connect`,
consentement accordé puis refusé, mot de passe refusé, vraie image d'écran).

## Utilisation en ligne de commande (réseau local)

**Sur la machine hôte** (celle qui sera observée) :

```bash
python -m rdlab.host --set-password          # une seule fois
python -m rdlab.host --bind 192.168.1.20     # votre IP LAN, pas 0.0.0.0
```

L'hôte affiche son identifiant machine et son **empreinte**. Notez l'empreinte.

**Sur la machine cliente** :

```bash
python -m rdlab.client 192.168.1.20
```

Le client affiche l'empreinte annoncée et un **code SAS à 6 chiffres**.
Comparez les deux avec ce qui s'affiche sur l'hôte *avant* d'accepter.
L'hôte demande alors son accord à la personne physiquement présente.

Échap ou la fermeture de la fenêtre termine la session.

### Commandes d'administration de l'hôte

```bash
python -m rdlab.host --status            # identifiant, empreinte, état
python -m rdlab.host --log 20            # 20 dernières entrées du journal
python -m rdlab.host --view-only         # observation seule (aucune entrée appliquée)
python -m rdlab.host --unattended on     # accès sans surveillance (opt-in explicite)
python -m rdlab.host --unattended off    # coupe-circuit
python -m rdlab.host --clear-password    # révoque l'accès + désactive le sans-surveillance
```

## Utilisation via votre VPS (« antenne »)

L'antenne remplit les deux rôles à la fois : **rendez-vous** (annuaire
`identifiant → hôte`) et **relais** (recopie d'octets). Ni l'hôte ni le client
n'ouvrent de port : **les deux se connectent en sortant**.

### Sur le VPS

Oui, un logiciel s'installe bien sur le VPS — mais **pas** l'interface. L'antenne
est un service *sans écran* : aucune capture, aucun affichage, aucune injection
clavier/souris. Elle recopie des octets chiffrés qu'elle ne peut pas lire.

Concrètement, ce qui est déposé sur le serveur : **5 modules Python**
(`__init__`, `protocol`, `crypto`, `identity`, `rendezvous`, ~60 Ko) et une
seule dépendance, `cryptography`. Ni `mss`, ni `pillow`, ni `numpy`, ni
`pynput`, ni `tkinter`. `app.py`, `capture.py`, `inject.py` et `client.py` sont
volontairement **exclus** : le VPS n'a aucune raison d'embarquer du code
capable de filmer un écran ou de piloter un clavier.

Depuis votre machine, trois commandes :

```bash
ssh root@VOTRE-VPS "mkdir -p /tmp/rdlab-install"
scp -r rdlab deploy root@VOTRE-VPS:/tmp/rdlab-install/
ssh -t root@VOTRE-VPS "cd /tmp/rdlab-install && sh deploy/install-antenne.sh"
```

L'installeur [`deploy/install-antenne.sh`](deploy/install-antenne.sh) crée un
utilisateur système sans shell, un venv, installe l'unité systemd durcie
([`deploy/rdlab-antenne.service`](deploy/rdlab-antenne.service) : utilisateur
non privilégié, `ProtectSystem=strict`, `MemoryMax`, `CPUQuota`), ouvre le port
si ufw ou firewalld est actif, démarre le service et vérifie qu'il tourne.

```bash
journalctl -u rdlab-antenne -f      # suivre en direct
systemctl stop rdlab-antenne        # couper
```

> **L'antenne est ouverte.** N'importe qui trouvant le port peut y enregistrer
> une machine et s'en servir comme relais gratuit — donc consommer votre bande
> passante. Il ne peut pas lire les sessions (chiffrement de bout en bout), mais
> il peut squatter le service. Tant qu'aucun contrôle d'accès n'est ajouté :
> gardez `--max-sessions` bas, et si vos IP sont fixes, restreignez le port à
> celles-ci (`ufw allow from <IP> to any port 7800 proto tcp`).

### Sur la machine hôte

```bash
python -m rdlab.host --relay vps.exemple.net:7800
```

### Sur la machine cliente

```bash
python -m rdlab.client "312 373 824" --via vps.exemple.net:7800
```

L'argument n'est plus une IP mais **l'identifiant machine à 9 chiffres**.

### Ce que l'antenne peut et ne peut pas

| | |
|---|---|
| Voit | les identifiants, les adresses IP, les horaires, les **volumes** |
| **Ne voit pas** | les pixels, les frappes, le mot de passe — tout est déjà chiffré de bout en bout avant d'y entrer |
| Ne peut pas | se faire passer pour l'hôte : la poignée de main exige une signature Ed25519 dont l'antenne n'a pas la clé privée |
| Peut | refuser ou couper une session (déni de service) — c'est la limite inhérente à tout relais |

**L'enregistrement est signé.** Sans cela, n'importe qui pourrait s'annoncer sous
votre identifiant et capter les demandes de connexion. L'antenne vérifie deux
choses : que l'identifiant dérive bien de la clé publique annoncée, et que le
détenteur signe un défi aléatoire. Elle applique en plus du TOFU côté serveur —
le premier à enregistrer un identifiant fixe la clé associée
(`--reset-registry` pour repartir de zéro).

### Limites de cette version

* **Relais systématique**, pas de perforation de NAT : tout le trafic passe par
  le VPS. Compter ~1 Mo par image clé et ~1 à 50 Ko par image différentielle.
  Surveillez votre quota de bande passante et gardez `--max-sessions` bas.
* **Une session à la fois par hôte** (`host.py` traite les sessions
  séquentiellement).
* **Pas de TLS sur le lien vers l'antenne.** Ce n'est pas un problème de
  confidentialité (la charge utile est déjà chiffrée de bout en bout, et le
  protocole d'enregistrement ne transporte aucun secret), mais du TLS cacherait
  les métadonnées aux intermédiaires réseau. `stunnel` ou `nginx stream` le
  fournissent sans toucher au code.
* L'étape suivante — STUN + perforation — supprimerait le relais dans ~80 % des
  cas et ne changerait **rien** au protocole applicatif.

## Les 10 étapes, et où les lire

| Étape | Fichier | Ce qu'on y apprend |
|---|---|---|
| 1. Connexion locale | `rdlab/protocol.py` | cadrage des messages sur un flux TCP |
| 2. Authentification | `rdlab/auth.py`, `rdlab/handshake.py` | défi/réponse, scrypt, liaison de canal |
| 3. Transmission d'image | `rdlab/capture.py` | tuiles, détection de changement, JPEG |
| 4. Souris | `rdlab/inject.py`, `client.py` | coordonnées normalisées, limitation de débit |
| 5. Clavier | `rdlab/inject.py` | down/up plutôt que caractères, raccourcis |
| 6. Chiffrement | `rdlab/crypto.py` | X25519 + HKDF + AES-GCM, nonce compteur |
| 7. Gestion de session | `rdlab/host.py`, `session_log.py` | consentement, permissions, journal |
| 8. Adaptation réseau | `rdlab/netadapt.py` | RTT, débit, jeter plutôt que mettre en file |
| 9. Antenne (VPS) | `rdlab/rendezvous.py` | rendez-vous signé, appariement par ticket, relais aveugle |
| 10. Application | `rdlab/app.py` | moteur découplé de l'interface, dialogues bloquants, non-furtivité |

Identité et révocation : `rdlab/identity.py` (Ed25519, TOFU, `known_hosts`).

## Mesures réelles observées (écran 2560×1440)

| Mesure | Valeur | Ce qu'elle enseigne |
|---|---|---|
| Image complète brute | 10,5 Mio | inenvisageable à 30 img/s |
| Image complète, 920 tuiles JPEG q70 | **940 Kio** | la compression seule fait ×11 |
| Bureau au repos, image suivante | **0 tuile envoyée** | la détection de changement fait le reste |
| Coût de la détection (Python pur) | **~40 ms** | plafond à ~25 img/s *avant même de compresser* |

Ces 40 ms sont exactement la raison pour laquelle les produits professionnels ne
comparent pas les pixels : ils demandent les rectangles sales au compositeur du
système (DXGI Desktop Duplication, portail PipeWire, CGDisplayStream), ce qui
ramène ce coût à zéro.

## Ce qui est simplifié ici, volontairement

| Ce labo | Un produit professionnel |
|---|---|
| JPEG par tuiles | H.264/H.265/AV1 avec prédiction inter-images, encodé sur GPU |
| TCP unique | UDP + protocole maison, ou QUIC/SRTP ; contrôle de congestion GCC/BBR |
| Rendez-vous + relais systématique | + STUN et perforation de NAT : ~80 % des sessions en direct |
| Verrou anti-rejeu par compteur | idem, plus renouvellement périodique des clés |
| Défi/réponse + scrypt | PAKE augmenté (SPAKE2+, OPAQUE) : aucune attaque hors ligne possible |
| Clé d'appareil en fichier 0600 | trousseau système (DPAPI/Keychain) ou TPM |
| Journal JSONL simple | journal chaîné par hash, rotation, export SIEM |
| Curseur dans le flux d'image | curseur en sprite séparé, dessiné côté client |
| Une seule session | multi-session, multi-écran, transfert de fichiers, audio |

## Évolution vers Internet

Le protocole applicatif (`PROTOCOL.md`) ne change **pas**. Seules deux briques
s'ajoutent en amont, et `socket.create_connection()` est remplacé par un socket
déjà connecté :

1. **Serveur de rendez-vous** : l'hôte maintient une connexion sortante et
   publie `identifiant → (adresse publique, port)`. Le client demande
   « où est 312 373 824 ? ». Ce serveur ne voit jamais le contenu de la session.
2. **Traversée de NAT** : les deux pairs apprennent leur adresse publique (STUN),
   puis émettent simultanément l'un vers l'autre (*hole punching*).
3. **Relais (TURN)** : si la perforation échoue (NAT symétrique des deux côtés),
   un serveur tiers recopie les octets. Il voit le volume et les horodatages,
   jamais le clair — le chiffrement est déjà de bout en bout.

Voir `PROTOCOL.md` pour les diagrammes.
