# Protocole rdlab v1

Protocole applicatif pédagogique. Générique, sans rapport avec un format
propriétaire.

## Format de trame

Avant la poignée de main (en clair, seulement `HELLO` / `HELLO_ACK`) :

```
 0        1                    5
 +--------+--------------------+--------------------------+
 | type   | longueur (uint32)  | payload                  |
 | 1 o.   | gros-boutiste      | 0 .. 8 Mio               |
 +--------+--------------------+--------------------------+
```

Après la poignée de main, **tout** est enveloppé :

```
 +--------------------+-------------------------------------------+
 | longueur (uint32)  | AES-256-GCM( type || longueur || payload ) |
 |                    | + tag d'authentification 16 o.            |
 +--------------------+-------------------------------------------+
     nonce = 0x00000000 || compteur 64 bits, un compteur par sens
```

Le type de message est **dans** le chiffré : un observateur ne distingue pas un
mouvement de souris d'un `PING`. Il ne voit que des tailles et des instants —
information résiduelle réelle (analyse de trafic), que seul un bourrage
(*padding*) réduirait.

## Table des messages

| Code | Message | Sens | Payload | Rôle |
|---|---|---|---|---|
| 0x01 | `HELLO` | C→H | JSON | version, nom du client, clé éphémère X25519 |
| 0x02 | `HELLO_ACK` | H→C | JSON | clé éphémère, identifiant machine, empreinte, clé publique Ed25519, **signature du transcript** |
| 0x10 | `AUTH_REQUEST` | H→C | JSON | sel scrypt, paramètres (n, r, p), défi aléatoire 32 o. |
| 0x11 | `AUTH_RESPONSE` | C→H | JSON | `HMAC(scrypt(pw, sel), défi ‖ liaison_de_canal)` |
| 0x14 | `CONSENT_PENDING` | H→C | JSON | « j'attends l'accord de l'humain » — évite que le client croie à un blocage |
| 0x12 | `SESSION_ACCEPTED` | H→C | JSON | permissions accordées + géométrie |
| 0x13 | `SESSION_REJECTED` | H→C | JSON | motif : `bad_password`, `consent_denied`, … |
| 0x20 | `SCREEN_INFO` | H→C | JSON | largeur, hauteur, taille de tuile |
| 0x21 | `SCREEN_FRAME` | H→C | binaire | une image : en-tête + N tuiles JPEG |
| 0x22 | `FRAME_ACK` | C→H | JSON | numéro d'image affichée (contrôle de débit) |
| 0x30 | `MOUSE_EVENT` | C→H | JSON | `{action, button, x, y, dx, dy}` — x, y ∈ [0,1] |
| 0x31 | `KEY_EVENT` | C→H | JSON | `{action: down\|up, keysym}` |
| 0x40 | `PING` | ↔ | JSON | jeton ; mesure du RTT, maintien de la session |
| 0x41 | `PONG` | ↔ | JSON | renvoie le jeton tel quel |
| 0x42 | `QUALITY_HINT` | C→H | JSON | `low` / `balanced` / `high` — une *suggestion*, l'hôte décide |
| 0x50 | `DISCONNECT` | ↔ | JSON | fin propre avec motif |

### Payload binaire de `SCREEN_FRAME`

```
 en-tête :   uint32 seq | uint16 nb_tuiles | uint8 qualité | uint8 drapeaux
 puis, nb_tuiles fois :
             uint16 x | uint16 y | uint16 w | uint16 h | uint32 taille
             <taille octets de JPEG>
```

`drapeaux & 0x01` = image clé (toutes les tuiles, pas seulement les modifiées).
Une image clé périodique résorbe toute désynchronisation accumulée.

## Séquence complète d'une connexion

```
CLIENT                                                      HÔTE
  |                                                           |
  |  TCP SYN / SYN-ACK / ACK                                  |
  |---------------------------------------------------------->|
  |                                                           |
  |  HELLO {v:1, name:"portable", eph_pub}                    |
  |---------------------------------------------------------->|
  |                                                           |
  |  HELLO_ACK {eph_pub, machine_id, fingerprint,             |
  |             device_pub, signature(transcript)}            |
  |<----------------------------------------------------------|
  |                                                           |
  | [vérifie la signature]                                    | [X25519]
  | [compare l'empreinte au known_hosts]                      | [HKDF]
  | [X25519 → HKDF → 4 clés]                                  |
  | [affiche le SAS : 418 902]                                | [SAS : 418 902]
  |                                                           |
  | ==================== CANAL CHIFFRÉ ====================== |
  |                                                           |
  |  AUTH_REQUEST {sel, n, r, p, défi}                        |
  |<----------------------------------------------------------|
  | [scrypt(mot de passe, sel) → k]                           |
  | [preuve = HMAC(k, défi ‖ liaison)]                        |
  |  AUTH_RESPONSE {preuve}                                   |
  |---------------------------------------------------------->|
  |                                        [recalcule, compare|
  |                                         en temps constant]|
  |  CONSENT_PENDING                                          |
  |<----------------------------------------------------------|
  |                                        [« Autoriser ? o/N »
  |                                          l'humain répond o]
  |  SCREEN_INFO {2560, 1440, 64}                             |
  |<----------------------------------------------------------|
  |  SESSION_ACCEPTED {allow_input:true}                      |
  |<----------------------------------------------------------|
  |                                                           |
  |  SCREEN_FRAME (image clé, 920 tuiles, 940 Kio)            |
  |<----------------------------------------------------------|
  |  SCREEN_FRAME (3 tuiles, 4 Kio)   ... en boucle           |
  |<----------------------------------------------------------|
  |  MOUSE_EVENT {move, x:0.41, y:0.62}                       |
  |---------------------------------------------------------->|
  |  MOUSE_EVENT {down, left, x:0.41, y:0.62}                 |
  |---------------------------------------------------------->|
  |  MOUSE_EVENT {up, left}                                   |
  |---------------------------------------------------------->|
  |  PING {token:91723}                                       |
  |<----------------------------------------------------------|
  |  PONG {token:91723}                                       |
  |---------------------------------------------------------->|
  |  DISCONNECT {reason:"client_closed"}                      |
  |---------------------------------------------------------->|
  |                                        [relâche toutes les
  |                                         touches, journalise]
```

## Protocole d'antenne (rendez-vous + relais)

Espace de codes distinct (`0x60+`), parlé **uniquement** entre un pair et le VPS.
Il s'arrête dès que le tuyau est ouvert : ensuite, le VPS ne fait que recopier
des octets qu'il ne comprend pas.

| Code | Message | Sens | Rôle |
|---|---|---|---|
| 0x60 | `REG_REQUEST` | hôte→VPS | identifiant + clé publique Ed25519 |
| 0x61 | `REG_CHALLENGE` | VPS→hôte | défi aléatoire 32 o. |
| 0x62 | `REG_PROOF` | hôte→VPS | `Ed25519_sign("rdlab-relay-registration-v1" ‖ défi)` |
| 0x63 | `REG_OK` | VPS→hôte | enregistré ; le canal de contrôle reste ouvert |
| 0x64 | `CONNECT_REQUEST` | client→VPS | « où est 312 373 824 ? » |
| 0x65 | `SESSION_OFFER` | VPS→hôte | « un client attend, ticket X » |
| 0x66 | `CLAIM` | hôte→VPS | sur une **connexion neuve** : « voici X » |
| 0x67 | `PEER_READY` | VPS→les deux | le tuyau est ouvert, plus un mot |
| 0x68 | `RELAY_ERROR` | VPS→x | `host_not_registered`, `bad_signature`, `relay_busy`, … |
| 0x69 | `RELAY_PING` | VPS→hôte | sonde de vivacité du canal de contrôle |

### Pourquoi deux connexions côté hôte

Le **canal de contrôle** (permanent) porte l'enregistrement et les offres. Les
**données** passent par une connexion neuve, réclamée avec un ticket à usage
unique (TTL 30 s). Conséquences : une session refusée ne casse pas
l'enregistrement, plusieurs sessions peuvent coexister, et le ticket empêche un
tiers de s'insérer dans un appariement en cours.

### Séquence

```
HÔTE                          VPS (antenne)                       CLIENT
  |                                |                                 |
  |--REG_REQUEST {id, clé pub}---->|                                 |
  |                                | vérifie : id == H(clé pub) ?    |
  |                                | TOFU : id déjà lié à une autre  |
  |                                |        clé ? -> REFUS           |
  |<--REG_CHALLENGE {défi}---------|                                 |
  |--REG_PROOF {signature}-------->|                                 |
  |                                | Ed25519.verify -> OK            |
  |<--REG_OK-----------------------|                                 |
  |                                |                                 |
  |   (canal de contrôle maintenu ouvert, RELAY_PING toutes les 30 s)|
  |                                |                                 |
  |                                |<--CONNECT_REQUEST {"312373824"}-|
  |                                | alloue un ticket, met en attente|
  |<--SESSION_OFFER {ticket}-------|                                 |
  |                                |                                 |
  |==NOUVELLE connexion TCP=======>|                                 |
  |--CLAIM {ticket}--------------->|                                 |
  |                                | apparie les deux sockets        |
  |<--PEER_READY-------------------|---PEER_READY------------------->|
  |                                |                                 |
  |<=========== le VPS recopie des octets, dans les deux sens ======>|
  |                                |                                 |
  |   À partir d'ici, HELLO / HELLO_ACK / AUTH_* / SCREEN_FRAME…     |
  |   se déroulent EXACTEMENT comme en direct. Le VPS ne voit         |
  |   que du chiffre : il n'a jamais eu les clés de session.          |
```

## Diagrammes

### 1. Connexion directe (ce labo, réseau local)

```
   +-------------+                                +-------------+
   |   CLIENT    |                                |    HÔTE     |
   | 192.168.1.9 |======== TCP 7700 ============> | 192.168.1.20|
   +-------------+      chiffré de bout en bout   +-------------+
                        aucun tiers impliqué
```

### 2. Connexion directe via Internet (après perforation de NAT)

```
  +--------+        +-----+          +----------+          +-----+     +--------+
  | CLIENT |--------| NAT |          | RENDEZ-  |          | NAT |-----| HÔTE   |
  +--------+        +-----+          |   VOUS   |          +-----+     +--------+
      |                |             +----------+             |            |
      |                |    (1) l'hôte reste connecté, publie son adresse  |
      |                |<--------------------------------------------------|
      |  (2) « où est 312 373 824 ? »                                      |
      |------------------------------>|                                    |
      |  (3) « 203.0.113.7:51820 », et l'hôte reçoit 198.51.100.4:44100    |
      |<------------------------------|----------------------------------->|
      |                                                                     |
      |  (4) les DEUX émettent en même temps vers l'autre : chaque NAT      |
      |      voit passer un paquet SORTANT et ouvre le retour              |
      |============================ ▶ ◀ ====================================|
      |                                                                     |
      |  (5) succès : le flux ne passe plus par le rendez-vous              |
      |<=================== média chiffré ==================================|
```

### 3. Connexion relayée (perforation impossible)

```
  +--------+                    +----------+                    +--------+
  | CLIENT |                    |  RELAIS  |                    |  HÔTE  |
  +--------+                    +----------+                    +--------+
      |                              |                              |
      |-- connexion SORTANTE ------->|<------ connexion SORTANTE ---|
      |   (le NAT l'autorise)        |   (le NAT l'autorise)        |
      |                              |                              |
      |== [chiffré C→H] ============>|=====> recopie les octets ===>|
      |<============ recopie <=======|<=========== [chiffré H→C] ===|
      |                              |                              |
      |                     le relais voit : volume, horaires, taille
      |                     le relais NE voit PAS : pixels, frappes, mot de passe
```

**Différence rendez-vous / relais** — les deux sont des serveurs tiers, mais :

| | Serveur de rendez-vous (signalisation) | Serveur relais (TURN) |
|---|---|---|
| Quand | avant la session, quelques kilo-octets | pendant toute la session |
| Rôle | traduire un identifiant en adresse, échanger les candidats | recopier les octets |
| Trafic | épisodique, minuscule | tout le flux vidéo |
| Coût | négligeable | dominant (bande passante) |
| Sait | qui parle à qui, et quand | idem, plus les volumes |
| Peut lire la session | non | **non plus** (chiffrement de bout en bout) |
| Toujours nécessaire | oui | non — seulement en repli (~10–20 % des cas) |

### 4. Authentification — pourquoi le MITM échoue

```
  Cas normal                          Cas attaquant au milieu
  ----------                          -----------------------
  CLIENT        HÔTE                  CLIENT      ATTAQUANT       HÔTE
    |             |                     |             |             |
    |<-- HELLO_ACK signé par            |<-- HELLO_ACK signé par ???
    |    la clé de l'hôte                |    l'attaquant n'a PAS
    |                                    |    la clé privée de l'hôte
    | signature valide ✓                 | signature invalide ✗  → ARRÊT
    |                                    |
    | SAS : 418 902                      | SAS côté client : 730 115
    |                                    | SAS côté hôte   : 264 883  → différents
    |                                    |
    | preuve = HMAC(k, défi ‖ liaison_1) | l'attaquant relaie la preuve vers l'hôte
    |                                    | mais sa liaison avec l'hôte vaut
    |                                    | liaison_2 ≠ liaison_1 → HMAC faux → ARRÊT
```

Trois barrières indépendantes : **signature** (certitude cryptographique),
**SAS** (filet humain à la première connexion), **liaison de canal** (empêche le
relais de la preuve). Il faut les casser toutes les trois.

### 5. Transmission d'une image

```
  HÔTE                                                            CLIENT
  ----                                                            ------
  [1] capture      mss.grab()  →  tableau 2560×1440×3
       |                             (10,5 Mio bruts)
  [2] découpage    40 × 23 = 920 tuiles de 64×64
       |
  [3] détection    CRC32 par tuile, comparé à l'image précédente
       |           bureau au repos → 0 tuile   ·   frappe → 2-4 tuiles
       |                                       ·   vidéo  → 900 tuiles
  [4] encodage     JPEG q70 sur les seules tuiles modifiées
       |
  [5] mise en file file de 2 — PLEINE ? on JETTE l'image
       |           (une image sautée est invisible ; une image
       |            en retard dégrade toute la session)
  [6] chiffrement  AES-GCM, nonce = compteur
       |
       +--- TCP --------------------------------------------------→ [7] déchiffrement
                                                                    [8] découpage des tuiles
                                                                    [9] JPEG → collage aux
                                                                        coordonnées (x, y)
                                                                   [10] mise à l'échelle,
                                                                        affichage
```

### 6. Transmission d'un clic

```
  CLIENT                                                         HÔTE
  ------                                                         ----
  [1] l'OS du client délivre un clic à la fenêtre
      → pixels DANS LA FENÊTRE : (512, 378)
       |
  [2] normalisation par rapport à la zone d'affichage de l'image
      → (0.4102, 0.6236)   ← indépendant des résolutions
       |
  [3] {"action":"down","button":"left","x":0.4102,"y":0.6236}
      ≈ 60 octets de JSON
       |
  [4] AES-GCM + longueur → ~90 octets sur le fil
       |
       +--- TCP (TCP_NODELAY : pas d'attente de Nagle) ------------→
                                                                    |
                                          [5] déchiffrement, ordre garanti par TCP
                                                                    |
                                          [6] autorisation : allow_input ?
                                              non → l'événement est IGNORÉ ICI
                                              (jamais côté client : un client
                                               modifié enverrait quand même)
                                                                    |
                                          [7] dénormalisation
                                              0.4102 × 2559 → x = 1049
                                              0.6236 × 1439 → y = 897
                                                                    |
                                          [8] mouse.position = (1049, 897)
                                              mouse.press(Button.left)
                                                → SendInput / XTEST / CGEvent
                                                                    |
                                          [9] l'OS distribue le clic à l'application
                                              qui se trouve sous ce point
                                                                    |
                                         [10] l'écran change → étapes 1-10 du
                                              diagramme précédent → retour visuel
```

Le **temps de boucle ressenti** est (3)+(4)+réseau+(5..9)+capture+encodage+
réseau+affichage. C'est pourquoi on ne met jamais les images en file d'attente :
tout retard accumulé sur le flux vidéo s'ajoute à la latence perçue du clic.

### 7. Chemin du clavier, et le cas des raccourcis

```
  Ctrl+C sur le client                        Injection sur l'hôte
  --------------------                        --------------------
  <KeyPress   keysym="Control_L">  --→  KEY_EVENT down Control_L  --→ press(Key.ctrl)
  <KeyPress   keysym="c">          --→  KEY_EVENT down c          --→ press('c')
  <KeyRelease keysym="c">          --→  KEY_EVENT up   c          --→ release('c')
  <KeyRelease keysym="Control_L">  --→  KEY_EVENT up   Control_L  --→ release(Key.ctrl)
```

On transmet **quatre** événements down/up, jamais « le caractère ^C ». Sinon :

* l'hôte ne saurait pas que Ctrl était maintenu ;
* les touches maintenues (Shift pour sélectionner, flèches dans un jeu) seraient
  impossibles ;
* deux dispositions clavier différentes (AZERTY / QWERTY) donneraient des
  résultats incohérents.

Corollaire de fiabilité : un `up` perdu laisse une touche **bloquée** sur l'hôte.
D'où deux règles dans le code : on ne limite jamais le débit des événements
down/up (seulement des déplacements), et `release_all()` est appelé sans
condition en fin de session.
