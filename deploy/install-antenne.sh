#!/bin/sh
# Installe l'antenne rdlab (rendez-vous + relais) sur un serveur Linux.
#
#   sudo sh deploy/install-antenne.sh
#   sudo sh deploy/install-antenne.sh --port 7800 --max-sessions 2
#
# A lancer DEPUIS la racine du depot rdlab, sur le VPS.
#
# CE QUI EST INSTALLE :
#   - un utilisateur systeme sans shell ni mot de passe : rdlab
#   - /opt/rdlab/rdlab/      le paquet Python (sans les modules graphiques)
#   - /opt/rdlab/venv/       un venv contenant uniquement `cryptography`
#   - /opt/rdlab/data/       le registre TOFU identifiant -> cle publique
#   - un service systemd durci, demarre au boot
#
# CE QUI N'EST PAS INSTALLE, ET N'A PAS A L'ETRE :
#   aucune interface graphique, aucune capture d'ecran, aucune injection
#   clavier/souris. L'antenne ne fait que recopier des octets chiffres
#   qu'elle ne peut pas lire. Elle n'a jamais les cles de session.

set -eu

PORT=7800
MAX_SESSIONS=2
RDUSER=rdlab
PREFIX=/opt/rdlab
DO_FIREWALL=1

usage() {
    cat <<USAGE
Usage: sudo sh deploy/install-antenne.sh [options]

  --port N            port d'ecoute (defaut: 7800)
  --max-sessions N    sessions relayees simultanees (defaut: 2)
                      garde-fou de bande passante : chaque session peut
                      consommer plusieurs Mo/s de votre quota VPS
  --user NOM          utilisateur systeme (defaut: rdlab)
  --prefix CHEMIN     repertoire d'installation (defaut: /opt/rdlab)
  --no-firewall       ne pas toucher a ufw/firewalld
  -h, --help          cette aide
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --max-sessions) MAX_SESSIONS="$2"; shift 2 ;;
        --user) RDUSER="$2"; shift 2 ;;
        --prefix) PREFIX="$2"; shift 2 ;;
        --no-firewall) DO_FIREWALL=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "option inconnue : $1" >&2; usage; exit 1 ;;
    esac
done

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m /!\\ %s\033[0m\n' "$1"; }
die() { printf '\033[1;31mERREUR: %s\033[0m\n' "$1" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "a lancer avec sudo"
[ -d "rdlab" ] && [ -f "rdlab/rendezvous.py" ] \
    || die "lancez ce script depuis la racine du depot rdlab (le dossier qui contient rdlab/rendezvous.py)"
[ -f "deploy/rdlab-antenne.service" ] || die "deploy/rdlab-antenne.service introuvable"

say "Verification de Python"
command -v python3 >/dev/null 2>&1 || die "python3 absent"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    || die "python3 >= 3.8 requis"
echo "    $(python3 --version)"

# Sur Debian/Ubuntu, `import venv` reussit alors que la creation echoue :
# c'est ensurepip qui est absent, livre dans un paquet separe dont le nom
# depend de la version (python3.14-venv, python3.12-venv...). On teste donc
# ensurepip, pas venv, et on installe le paquet versionne en priorite.
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    say "ensurepip absent : installation de python${PYVER}-venv"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq || true
        apt-get install -y -qq "python${PYVER}-venv" \
            || apt-get install -y -qq python3-venv \
            || die "installez python${PYVER}-venv puis relancez"
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q python3-venv || die "installez le venv de python3 puis relancez"
    else
        die "ensurepip manquant : installez le paquet venv de python3, puis relancez"
    fi
    python3 -c 'import ensurepip' >/dev/null 2>&1 \
        || die "ensurepip toujours absent apres installation"
fi

say "Utilisateur systeme : $RDUSER"
if id "$RDUSER" >/dev/null 2>&1; then
    echo "    existe deja"
else
    useradd --system --no-create-home --home-dir "$PREFIX" \
            --shell /usr/sbin/nologin "$RDUSER" 2>/dev/null \
        || useradd --system --no-create-home --home-dir "$PREFIX" \
                   --shell /sbin/nologin "$RDUSER"
    echo "    cree (sans shell, sans mot de passe)"
fi

say "Copie du paquet dans $PREFIX"
mkdir -p "$PREFIX/rdlab" "$PREFIX/data"
# Seuls les modules dont l'antenne a besoin. app.py, capture.py, inject.py
# et client.py sont volontairement exclus : le VPS ne doit pas embarquer de
# code d'interface, de capture d'ecran ni d'injection d'entrees.
for f in __init__.py protocol.py crypto.py identity.py rendezvous.py; do
    [ -f "rdlab/$f" ] || die "rdlab/$f manquant"
    cp "rdlab/$f" "$PREFIX/rdlab/$f"
done
rm -f "$PREFIX/rdlab/__main__.py"
echo "    5 modules copies (aucun code graphique, de capture ou d'injection)"

say "Environnement Python"
# Une creation de venv interrompue laisse un bin/python utilisable mais pas
# de pip. Tester la seule presence de l'interpreteur ferait sauter l'etape
# et echouer plus loin : on valide pip, et on reconstruit si besoin.
if [ -d "$PREFIX/venv" ] && ! "$PREFIX/venv/bin/pip" --version >/dev/null 2>&1; then
    warn "environnement incomplet detecte, reconstruction"
    rm -rf "$PREFIX/venv"
fi
if [ ! -x "$PREFIX/venv/bin/pip" ]; then
    python3 -m venv "$PREFIX/venv" || die "creation du venv impossible"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "cryptography>=42"
echo "    cryptography $("$PREFIX/venv/bin/python" -c 'import cryptography; print(cryptography.__version__)')"

say "Verification du chargement"
( cd "$PREFIX" && "$PREFIX/venv/bin/python" -c \
    'from rdlab import rendezvous; print("    rdlab.rendezvous charge, port par defaut", rendezvous.DEFAULT_PORT)' ) \
    || die "le paquet ne se charge pas"

chown -R "$RDUSER:$RDUSER" "$PREFIX"
chmod 700 "$PREFIX/data"

say "Service systemd"
sed -e "s|^User=.*|User=$RDUSER|" \
    -e "s|^Group=.*|Group=$RDUSER|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$PREFIX|" \
    -e "s|^Environment=RDLAB_DATA=.*|Environment=RDLAB_DATA=$PREFIX/data|" \
    -e "s|^ReadWritePaths=.*|ReadWritePaths=$PREFIX/data|" \
    -e "s|^ExecStart=.*|ExecStart=$PREFIX/venv/bin/python -m rdlab.rendezvous --bind 0.0.0.0 --port $PORT --max-sessions $MAX_SESSIONS|" \
    deploy/rdlab-antenne.service > /etc/systemd/system/rdlab-antenne.service
systemctl daemon-reload
systemctl enable --now rdlab-antenne
sleep 2

if [ "$DO_FIREWALL" = "1" ]; then
    say "Pare-feu"
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
        ufw allow "$PORT/tcp" >/dev/null && echo "    ufw : port $PORT/tcp ouvert"
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null
        firewall-cmd --reload >/dev/null
        echo "    firewalld : port $PORT/tcp ouvert"
    else
        warn "aucun pare-feu actif detecte. Si votre hebergeur en a un dans"
        warn "son panneau de controle, ouvrez-y le port $PORT/tcp."
    fi
fi

say "Etat du service"
systemctl is-active --quiet rdlab-antenne \
    && echo "    actif" \
    || { journalctl -u rdlab-antenne -n 30 --no-pager; die "le service n'a pas demarre"; }
systemctl --no-pager --lines=8 status rdlab-antenne || true

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<FIN

-------------------------------------------------------------------
 Antenne installee et demarree.

   adresse a saisir dans rdlab : ${IP:-<IP-DE-VOTRE-VPS>}:$PORT
   journal en direct           : journalctl -u rdlab-antenne -f
   arret                       : systemctl stop rdlab-antenne
   desinstallation             : systemctl disable --now rdlab-antenne
                                 rm /etc/systemd/system/rdlab-antenne.service
                                 rm -rf $PREFIX

 A LIRE : cette antenne est OUVERTE.
   N'importe qui trouvant le port $PORT peut y enregistrer une machine et
   s'en servir comme relais gratuit, donc consommer votre bande passante.
   Elle ne peut pas lire les sessions (tout est chiffre de bout en bout),
   mais elle peut etre squattee.

   Tant qu'aucun controle d'acces n'est ajoute, limitez le risque :
     - gardez --max-sessions bas (actuellement $MAX_SESSIONS) ;
     - si vos IP sont fixes, restreignez le port a celles-ci, par ex. :
         ufw delete allow $PORT/tcp
         ufw allow from <VOTRE-IP> to any port $PORT proto tcp
     - surveillez : journalctl -u rdlab-antenne | grep ENREGISTRE
-------------------------------------------------------------------
FIN
