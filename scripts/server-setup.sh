#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# NEXYA Backend — Bootstrap VPS production (Hetzner CX32, Ubuntu 24.04 LTS)
# ══════════════════════════════════════════════════════════════════════════════
#
# Prépare un VPS Ubuntu 24.04 VIERGE à recevoir la stack NEXYA :
#   1. Mise à jour du système (apt)
#   2. Docker CE + plugin docker compose
#   3. Swapfile 2 Go (filet RAM sur le CX32 8 Go)
#   4. Pare-feu UFW — ports 22 (SSH), 80, 443 SEULEMENT
#   5. fail2ban (anti brute-force SSH)
#   6. unattended-upgrades (mises à jour de sécurité automatiques)
#   7. Durcissement SSH (désactivation de l'auth par mot de passe)
#   8. Arborescence /opt/nexya (+ backups + secrets)
#
# IDEMPOTENT : chaque étape vérifie son état avant d'agir. Relancer le script
# ne casse rien.
#
# USAGE (en tant que root sur le VPS) :
#   bash server-setup.sh
#
# ⚠️  L'étape 7 désactive l'authentification SSH par mot de passe. Le script
#     ne le fait QUE si une clé SSH publique est déjà installée pour root
#     (/root/.ssh/authorized_keys non vide) — sinon il saute l'étape avec un
#     avertissement, pour ne JAMAIS te verrouiller hors du serveur.
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

log()  { echo "[server-setup] $*"; }
warn() { echo "[server-setup] ⚠️  $*" >&2; }

# ── Pré-check : root obligatoire ──────────────────────────────────────────────
if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR : ce script doit être lancé en tant que root." >&2
  exit 1
fi

# ── 1. Mise à jour du système ─────────────────────────────────────────────────
log "1/8 — Mise à jour apt"
apt-get update -y
apt-get upgrade -y

log "Installation des paquets de base"
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  gnupg \
  ufw \
  fail2ban \
  unattended-upgrades

# ── 2. Docker CE + plugin compose ─────────────────────────────────────────────
if command -v docker >/dev/null 2>&1; then
  log "2/8 — Docker déjà installé ($(docker --version)) — skip"
else
  log "2/8 — Installation de Docker CE (script officiel get.docker.com)"
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
fi
systemctl enable --now docker
log "Docker : $(docker --version)"
log "Compose : $(docker compose version)"

# ── 3. Swapfile 2 Go ──────────────────────────────────────────────────────────
if swapon --show 2>/dev/null | grep -q '/swapfile'; then
  log "3/8 — Swapfile déjà actif — skip"
else
  log "3/8 — Création d'un swapfile de 2 Go"
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  if ! grep -q '/swapfile' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  log "Swapfile actif : $(swapon --show | grep /swapfile)"
fi

# ── 4. Pare-feu UFW ───────────────────────────────────────────────────────────
log "4/8 — Configuration du pare-feu UFW (22, 80, 443 uniquement)"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP (Caddy → redirige vers HTTPS)
ufw allow 443/tcp   # HTTPS (Caddy)
ufw --force enable
log "UFW actif :"
ufw status verbose | sed 's/^/[server-setup]   /'

# ── 5. fail2ban ───────────────────────────────────────────────────────────────
log "5/8 — Activation de fail2ban (anti brute-force SSH)"
systemctl enable --now fail2ban

# ── 6. unattended-upgrades (mises à jour de sécurité automatiques) ────────────
log "6/8 — Activation des mises à jour de sécurité automatiques"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades

# ── 7. Durcissement SSH ───────────────────────────────────────────────────────
log "7/8 — Durcissement SSH"
if [[ -s /root/.ssh/authorized_keys ]]; then
  cat > /etc/ssh/sshd_config.d/99-nexya-hardening.conf <<'EOF'
# NEXYA — durcissement SSH (server-setup.sh)
PasswordAuthentication no
PermitRootLogin prohibit-password
EOF
  systemctl restart ssh
  log "SSH durci : authentification par mot de passe désactivée (clé uniquement)"
else
  warn "Aucune clé SSH dans /root/.ssh/authorized_keys —"
  warn "durcissement SSH SAUTÉ pour ne pas te verrouiller hors du serveur."
  warn "Ajoute ta clé publique puis relance ce script."
fi

# ── 8. Arborescence /opt/nexya ────────────────────────────────────────────────
log "8/8 — Création de l'arborescence /opt/nexya"
mkdir -p /opt/nexya/backups /opt/nexya/secrets
chmod 700 /opt/nexya/secrets   # secrets : accessible uniquement par root
log "Arborescence prête : /opt/nexya, /opt/nexya/backups, /opt/nexya/secrets"

# ── Récapitulatif ─────────────────────────────────────────────────────────────
log "═══════════════════════════════════════════════════════════"
log "✅ Bootstrap VPS terminé."
log "   Docker      : $(docker --version)"
log "   Swap        : $(free -h | awk '/Swap:/ {print $2}')"
log "   UFW         : $(ufw status | head -1)"
log ""
log "Étape suivante : cloner le repo dans /opt/nexya (phase D7)."
log "═══════════════════════════════════════════════════════════"
