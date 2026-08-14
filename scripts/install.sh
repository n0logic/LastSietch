#!/usr/bin/env bash
#
# install.sh - automated Debian self-host installer for a Dune: Awakening
# community server. Automates the docs/01-install.md flow (steps 1-9) end to
# end: host prep, steamcmd, k3s, the Funcom bootstrap payload, the world
# wizard, and a wait-until-Healthy check.
#
# Sources the Funcom server payload directly with steamcmd (anonymous login),
# so no Windows install of the Self-Hosted Server tool is needed.
#
# Usage:
#   sudo ./install.sh --jwt <token> --world "My World" --region 2 --ip auto
#   curl -fsSL <raw-url>/scripts/install.sh | sudo bash -s -- --jwt <token> ...
#
# Run with --help for all flags. Safe to re-run: existing steps are detected
# and skipped, and it refuses to clobber an already-Healthy server unless
# --force is given.
#
set -euo pipefail

VERSION="0.1.0"
APPID_DEFAULT="4754530"   # GA "Dune: Awakening Self-Hosted Server". PTC was 3104830.
MIN_DISK_GB=30

# ---- defaults / flags -------------------------------------------------------
DUNE_USER="dune"
WORLD=""
REGION=""
JWT=""
PUBLIC_IP=""
APPID="$APPID_DEFAULT"
ASSUME_YES=false
NON_INTERACTIVE=false
DRY_RUN=false
FORCE=false
SKIP_K3S=false
SKIP_STEAMCMD=false

# ---- output helpers ---------------------------------------------------------
if [[ -t 1 ]]; then
    C_G='\033[0;32m'; C_R='\033[0;31m'; C_Y='\033[0;33m'; C_B='\033[1m'; C_N='\033[0m'
else
    C_G=''; C_R=''; C_Y=''; C_B=''; C_N=''
fi
info()  { printf '%b\n' "${C_G}[*]${C_N} $*"; }
warn()  { printf '%b\n' "${C_Y}[!]${C_N} $*" >&2; }
err()   { printf '%b\n' "${C_R}[x]${C_N} $*" >&2; }
die()   { err "$*"; exit 1; }
step()  { printf '\n%b\n' "${C_B}== $* ==${C_N}"; }

# Run a command, or just print it under --dry-run.
run() {
    if $DRY_RUN; then
        printf '    [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

usage() {
    cat <<EOF
Dune: Awakening self-host installer v${VERSION}

Automates a full Debian install of a community game server (docs/01-install.md).

Required:
  --jwt <token>        Self-hosted token from your Dune account page (the wizard
                       derives your HostId and server namespace from it).
  --world <name>       World name shown in the server browser (1-50 chars).

Optional:
  --region <1|2>       1 = Europe, 2 = North America. Prompted if omitted.
  --ip <addr|auto>     Public IP players connect to. "auto" detects it.
                       Prompted/auto-detected if omitted.
  --dune-user <name>   Service account to create/use (default: dune).
  --appid <id>         Steam app id for the server tool (default: ${APPID_DEFAULT}).
  -y, --yes            Assume "yes" to confirmations (still prompts for missing
                       required values unless --non-interactive).
  --non-interactive    Never prompt; fail if a required value is missing.
  --skip-steamcmd      Assume steamcmd is already installed.
  --skip-k3s           Assume k3s is already installed and running.
  --force              Proceed even if a Healthy server already exists.
  --dry-run            Print what would run; change nothing.
  -h, --help           This help.

Examples:
  sudo ./install.sh --jwt eyJ... --world "Sietch Tabr" --region 2 --ip auto
  sudo ./install.sh --jwt eyJ... --world "EU Home" --region 1 --ip 203.0.113.10 -y
EOF
}

# ---- arg parsing ------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --jwt)             JWT="${2:-}"; shift 2;;
        --world)           WORLD="${2:-}"; shift 2;;
        --region)          REGION="${2:-}"; shift 2;;
        --ip)              PUBLIC_IP="${2:-}"; shift 2;;
        --dune-user)       DUNE_USER="${2:-}"; shift 2;;
        --appid)           APPID="${2:-}"; shift 2;;
        -y|--yes)          ASSUME_YES=true; shift;;
        --non-interactive) NON_INTERACTIVE=true; ASSUME_YES=true; shift;;
        --skip-steamcmd)   SKIP_STEAMCMD=true; shift;;
        --skip-k3s)        SKIP_K3S=true; shift;;
        --force)           FORCE=true; shift;;
        --dry-run)         DRY_RUN=true; shift;;
        -h|--help)         usage; exit 0;;
        *) die "Unknown argument: $1 (see --help)";;
    esac
done

DUNE_HOME="/home/${DUNE_USER}"
DUNE_DIR="${DUNE_HOME}/.dune"
DOWNLOAD="${DUNE_DIR}/download"
SETTINGS="${DUNE_DIR}/settings.conf"
LOG="/tmp/dune-install-$(id -un).log"

# ---- small utilities --------------------------------------------------------
confirm() {
    # confirm "prompt" -> 0 if yes
    local prompt="$1"
    $ASSUME_YES && return 0
    $NON_INTERACTIVE && return 0
    local reply
    read -r -p "$prompt [y/N] " reply </dev/tty || return 1
    [[ "${reply,,}" == "y" || "${reply,,}" == "yes" ]]
}

prompt_value() {
    # prompt_value "label" varname  -> reads into the named var from the tty
    local label="$1" __var="$2" __val=""
    if $NON_INTERACTIVE; then
        die "Missing required value: $label (running --non-interactive)"
    fi
    read -r -p "$label: " __val </dev/tty || die "No input for: $label"
    printf -v "$__var" '%s' "$__val"
}

as_dune() { sudo -u "$DUNE_USER" -H "$@"; }

detect_public_ip() {
    local ip url
    for url in https://api.ipify.org https://ifconfig.me https://icanhazip.com; do
        ip=$(curl -fsS --max-time 10 "$url" 2>/dev/null | tr -d '[:space:]') || true
        if [[ "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            printf '%s' "$ip"; return 0
        fi
    done
    return 1
}

# ---- base packages ----------------------------------------------------------
ensure_base_packages() {
    # Minimal Debian ships without curl/jq/sudo, yet this script's own IP
    # detection and the k3s installer need curl, and Funcom's world wizard needs
    # jq. Install them before anything else relies on them.
    if command -v curl >/dev/null 2>&1 && command -v jq >/dev/null 2>&1 \
        && command -v bc >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
        info "Base packages present (curl, jq, bc, sudo)."
        return 0
    fi
    info "Installing base packages (curl, ca-certificates, jq, bc, sudo)..."
    if command -v apt-get >/dev/null 2>&1; then
        run apt-get update
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
            curl ca-certificates jq bc sudo
    else
        warn "No apt-get found; install curl, jq, bc, sudo manually before continuing."
    fi
}

# ---- preflight --------------------------------------------------------------
preflight() {
    step "Preflight"

    [[ "$(id -u)" -eq 0 ]] || die "Run as root (use sudo)."

    # OS detection
    [[ -r /etc/os-release ]] || die "Cannot read /etc/os-release."
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_VER="${VERSION_ID:-unknown}"
    info "OS: ${PRETTY_NAME:-$OS_ID $OS_VER}   arch: $(uname -m)"
    case "$OS_ID" in
        debian) : ;;
        ubuntu) warn "Ubuntu is best-effort; this installer is validated on Debian 12/13." ;;
        *) warn "Untested distro '$OS_ID'. Package steps may need adjustment." ;;
    esac

    # Base utilities the rest of preflight (and later steps) rely on.
    ensure_base_packages

    # Disk space on /
    local avail
    avail=$(df -B1G -P / | awk 'NR==2 {print $4+0}')
    if [[ "${avail:-0}" -lt "$MIN_DISK_GB" ]]; then
        die "Only ${avail}G free on / ; need at least ${MIN_DISK_GB}G (server payload + images)."
    fi
    info "Disk: ${avail}G free on / (need ${MIN_DISK_GB}G)"

    # Required inputs
    [[ -n "$WORLD" ]] || prompt_value "World name (1-50 chars)" WORLD
    [[ ${#WORLD} -ge 1 && ${#WORLD} -le 50 ]] || die "World name must be 1-50 characters."

    if [[ -z "$REGION" ]]; then
        if $NON_INTERACTIVE; then die "Missing --region (1=Europe, 2=North America)."; fi
        echo "Select region: 1) Europe   2) North America"
        prompt_value "Region [1/2]" REGION
    fi
    case "$REGION" in
        1|2) : ;;
        *) die "Region must be 1 (Europe) or 2 (North America).";;
    esac

    [[ -n "$JWT" ]] || prompt_value "Self-hosted token (JWT from your Dune account page)" JWT
    # Light sanity check; world.sh does the authoritative validation.
    [[ "$JWT" == *.*.* ]] || die "That does not look like a JWT (expected three dot-separated parts)."

    # Public IP
    if [[ -z "$PUBLIC_IP" || "$PUBLIC_IP" == "auto" ]]; then
        info "Detecting public IP..."
        if PUBLIC_IP=$(detect_public_ip); then
            info "Detected public IP: ${C_B}${PUBLIC_IP}${C_N}"
            confirm "Use ${PUBLIC_IP} as the address players connect to?" \
                || prompt_value "Public IP" PUBLIC_IP
        else
            warn "Could not auto-detect a public IP."
            prompt_value "Public IP players connect to" PUBLIC_IP
        fi
    fi
    [[ "$PUBLIC_IP" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] \
        || die "Public IP '$PUBLIC_IP' is not a valid IPv4 address."

    info "World '${WORLD}', region ${REGION}, IP ${PUBLIC_IP}, user '${DUNE_USER}', appid ${APPID}"
}

# Refuse to clobber a server that is already up, unless --force.
guard_existing_server() {
    command -v kubectl >/dev/null 2>&1 || return 0
    local ns
    ns=$(kubectl get ns -o name 2>/dev/null | sed 's#namespace/##' | grep '^funcom-seabass-sh-' | head -n1 || true)
    [[ -n "$ns" ]] || return 0
    if kubectl get battlegroups -n "$ns" --no-headers 2>/dev/null | grep -qiw Healthy; then
        if $FORCE; then
            warn "A Healthy server ($ns) already exists; continuing because --force."
        else
            die "A Healthy server already exists in namespace '$ns'. Re-run with --force to proceed anyway."
        fi
    fi
}

# ---- Step 1: dune user ------------------------------------------------------
step_create_user() {
    step "Step 1/9  Create the '${DUNE_USER}' user"
    if id "$DUNE_USER" >/dev/null 2>&1; then
        info "User '${DUNE_USER}' already exists."
    else
        run adduser "$DUNE_USER" --gecos "Dune Self-Host,,," --disabled-password
        run usermod -aG sudo "$DUNE_USER"
    fi
    if [[ ! -f "/etc/sudoers.d/${DUNE_USER}" ]]; then
        if $DRY_RUN; then
            printf '    [dry-run] write /etc/sudoers.d/%s (NOPASSWD)\n' "$DUNE_USER"
        else
            echo "${DUNE_USER} ALL=(ALL:ALL) NOPASSWD: ALL" > "/etc/sudoers.d/${DUNE_USER}"
            chmod 440 "/etc/sudoers.d/${DUNE_USER}"
        fi
    else
        info "Passwordless sudo already configured."
    fi
}

# ---- Step 2: steamcmd -------------------------------------------------------
enable_apt_components() {
    # Enable the non-free / multiverse component that ships steamcmd.
    case "$OS_ID" in
        debian)
            if [[ "${OS_VER%%.*}" -ge 13 ]] || [[ -f /etc/apt/sources.list.d/debian.sources ]]; then
                # Debian 13+: deb822 format. Some installs still ship the legacy
                # one-line format, so convert first (creates debian.sources).
                if [[ ! -f /etc/apt/sources.list.d/debian.sources ]] && command -v apt >/dev/null; then
                    info "Converting legacy APT sources to deb822 (apt modernize-sources)..."
                    run bash -c 'yes | apt modernize-sources >/dev/null 2>&1 || true'
                fi
                if [[ -f /etc/apt/sources.list.d/debian.sources ]]; then
                    run sed -i 's/^Components: main.*/Components: main contrib non-free non-free-firmware/' \
                        /etc/apt/sources.list.d/debian.sources
                else
                    run sed -i -E 's/^(deb .*(main))( .*)?$/\1 contrib non-free non-free-firmware/' /etc/apt/sources.list
                fi
            else
                # Debian 12: legacy one-line format.
                run sed -i -E 's/^(deb .*debian.* main)( .*)?$/\1 contrib non-free non-free-firmware/' /etc/apt/sources.list
            fi
            ;;
        ubuntu)
            run add-apt-repository -y multiverse
            ;;
        *)
            warn "Unknown distro: enable the component that provides 'steamcmd' manually if the install fails."
            ;;
    esac
}

step_install_steamcmd() {
    step "Step 2/9  Install steamcmd"
    if $SKIP_STEAMCMD || command -v steamcmd >/dev/null 2>&1; then
        info "steamcmd already present; skipping component/arch setup."
        return
    fi
    enable_apt_components
    run dpkg --add-architecture i386
    run apt-get update
    # Pre-accept the Steam license so the install is non-interactive.
    if ! $DRY_RUN; then
        echo steam steam/question select "I AGREE" | debconf-set-selections
        echo steam steam/license note '' | debconf-set-selections
    fi
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        steamcmd lib32gcc-s1 bc jq openssl curl ca-certificates
    command -v steamcmd >/dev/null 2>&1 || $DRY_RUN \
        || die "steamcmd still not found. Confirm non-free/multiverse + i386 are enabled (apt-cache policy steamcmd)."
    info "steamcmd installed."
}

# ---- rc-service / rc-update shim -------------------------------------------
install_rc_shim() {
    # Funcom's k3s.sh calls `rc-service k3s start` and `rc-update add k3s`
    # (OpenRC). On systemd Debian those commands do not exist. Provide thin
    # shims that translate to systemctl so the wizard runs unmodified.
    command -v systemctl >/dev/null 2>&1 || return 0
    command -v rc-service >/dev/null 2>&1 && command -v rc-update >/dev/null 2>&1 && {
        info "rc-service/rc-update already available; not shimming."
        return 0
    }
    step "Install rc-service/rc-update -> systemctl shim (for Funcom's k3s stage)"
    if $DRY_RUN; then
        printf '    [dry-run] write /usr/local/bin/rc-service and /usr/local/bin/rc-update\n'
        return
    fi
    cat > /usr/local/bin/rc-service <<'SHIM'
#!/bin/sh
# Shim: `rc-service <svc> <action>` -> `systemctl <action> <svc>`
svc="$1"; action="$2"
[ -z "$action" ] && action="status"
exec systemctl "$action" "$svc"
SHIM
    cat > /usr/local/bin/rc-update <<'SHIM'
#!/bin/sh
# Shim: `rc-update add|del <svc> [runlevel]` -> systemctl enable|disable
action="$1"; svc="$2"
case "$action" in
    add) exec systemctl enable "$svc" ;;
    del) exec systemctl disable "$svc" ;;
    *)   exit 0 ;;
esac
SHIM
    chmod +x /usr/local/bin/rc-service /usr/local/bin/rc-update
    info "Shim installed."
}

# ---- Step 3: k3s ------------------------------------------------------------
step_install_k3s() {
    step "Step 3/9  Install k3s (external IP ${PUBLIC_IP})"
    if $SKIP_K3S || systemctl is-active --quiet k3s 2>/dev/null; then
        info "k3s already active; skipping install."
    else
        if $DRY_RUN; then
            printf '    [dry-run] curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--node-external-ip %s" sh -\n' "$PUBLIC_IP"
        else
            curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--node-external-ip ${PUBLIC_IP}" sh -
        fi
    fi
    # Wait for the node to be Ready.
    if ! $DRY_RUN; then
        info "Waiting for k3s node to be Ready..."
        local i
        for ((i=0; i<90; i++)); do
            if kubectl get nodes 2>/dev/null | grep -qw Ready; then
                kubectl get nodes
                return 0
            fi
            sleep 2
        done
        die "k3s node did not become Ready in ~180s. Check: journalctl -u k3s"
    fi
}

# ---- Step 4: containerd symlink --------------------------------------------
step_containerd_symlink() {
    step "Step 4/9  Persist the containerd socket symlink"
    if $DRY_RUN; then
        printf '    [dry-run] write /etc/tmpfiles.d/k3s-containerd-symlink.conf + systemd-tmpfiles --create\n'
        return
    fi
    printf 'L /run/containerd /run/k3s/containerd\n' > /etc/tmpfiles.d/k3s-containerd-symlink.conf
    systemd-tmpfiles --create /etc/tmpfiles.d/k3s-containerd-symlink.conf || true
    ls -la /run/containerd || warn "/run/containerd not present yet; it appears once k3s containerd starts."
}

# ---- Step 5: dune dirs ------------------------------------------------------
step_dune_dirs() {
    step "Step 5/9  Pre-stage ${DUNE_DIR}"
    run as_dune mkdir -p "${DUNE_DIR}/bin"
    run as_dune mkdir -p "${DUNE_DIR}/download"
}

# ---- Step 6: settings.conf --------------------------------------------------
step_settings_conf() {
    step "Step 6/9  Write settings.conf (${PUBLIC_IP})"
    if [[ -f "$SETTINGS" ]] && ! $DRY_RUN; then
        cp -a "$SETTINGS" "${SETTINGS}.bak-$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || echo prev)" 2>/dev/null || true
    fi
    if $DRY_RUN; then
        printf '    [dry-run] write %s with public IP %s\n' "$SETTINGS" "$PUBLIC_IP"
    else
        # Wizard format: three blank lines then the IP.
        as_dune bash -c "printf '\n\n\n%s\n' '$PUBLIC_IP' > '$SETTINGS'"
    fi
}

# ---- Step 7: pull the Funcom payload (no Windows) ---------------------------
step_pull_payload() {
    step "Step 7/9  Download the Funcom server payload via steamcmd (appid ${APPID})"
    if [[ -f "${DOWNLOAD}/scripts/setup.sh" && -f "${DOWNLOAD}/scripts/battlegroup.sh" ]]; then
        info "Payload already present (${DOWNLOAD}/scripts/setup.sh); skipping download."
        return
    fi
    info "Pulling ~5 GB (server payload + Steam Linux runtime). This can take a while."
    if $DRY_RUN; then
        printf '    [dry-run] as %s: steamcmd +force_install_dir %s +login anonymous +app_update %s +quit\n' \
            "$DUNE_USER" "$DOWNLOAD" "$APPID"
    else
        local attempt
        for attempt in 1 2 3; do
            if as_dune steamcmd +set_spew_level 1 1 +force_install_dir "$DOWNLOAD" \
                    +login anonymous +app_update "$APPID" +logoff +quit; then
                break
            fi
            warn "steamcmd attempt ${attempt} failed."
            [[ "$attempt" -eq 3 ]] && die "steamcmd failed 3 times. Retry later (Steam CDN may be rate-limiting)."
            sleep 15
        done
        [[ -f "${DOWNLOAD}/scripts/setup.sh" ]] \
            || die "Download completed but ${DOWNLOAD}/scripts/setup.sh is missing. Wrong appid? (GA = ${APPID_DEFAULT})"
    fi
}

# ---- Step 8: run the wizard non-interactively -------------------------------
step_run_bootstrap() {
    step "Step 8/9  Run the Funcom setup wizard (world '${WORLD}', region ${REGION})"
    # system.sh symlinks battlegroup/bg-util with `ln -s` (no -f); clear stale
    # links so a re-run does not error out.
    if ! $DRY_RUN; then
        rm -f "${DUNE_DIR}/bin/battlegroup" "${DUNE_DIR}/bin/bg-util" 2>/dev/null || true
    fi
    if $DRY_RUN; then
        printf '    [dry-run] pipe [world/region/jwt] into %s/scripts/setup.sh (as %s)\n' "$DOWNLOAD" "$DUNE_USER"
        return
    fi
    info "Driving the wizard; full log at ${LOG}"
    # world.sh reads three lines in order: world name, region number, JWT.
    # printf is a builtin, so the JWT never lands in a process argv.
    set +e
    printf '%s\n%s\n%s\n' "$WORLD" "$REGION" "$JWT" \
        | sudo -u "$DUNE_USER" -H bash "${DOWNLOAD}/scripts/setup.sh" 2>&1 | tee "$LOG"
    local rc=${PIPESTATUS[1]}
    set -e
    [[ "$rc" -eq 0 ]] || warn "setup.sh exited ${rc}; checking cluster state before deciding it failed."
}

# ---- Step 9: wait for Healthy ----------------------------------------------
step_wait_healthy() {
    step "Step 9/9  Wait for the Battlegroup to become Healthy"
    if $DRY_RUN; then
        printf '    [dry-run] poll battlegroups until Healthy\n'
        return
    fi
    local ns i phase
    ns=""
    for ((i=0; i<30; i++)); do
        ns=$(kubectl get ns -o name 2>/dev/null | sed 's#namespace/##' \
            | grep '^funcom-seabass-sh-' | head -n1 || true)
        [[ -n "$ns" ]] && break
        sleep 2
    done
    [[ -n "$ns" ]] || die "No funcom-seabass-sh-* namespace was created. Review ${LOG}."
    info "Server namespace: ${C_B}${ns}${C_N}"

    info "Polling for Healthy (first boot extracts images and is slow; up to ~15 min)..."
    for ((i=0; i<90; i++)); do
        if kubectl get battlegroups -n "$ns" --no-headers 2>/dev/null | grep -qiw Healthy; then
            info "${C_G}Battlegroup is Healthy.${C_N}"
            kubectl get battlegroups -n "$ns" 2>/dev/null || true
            print_summary "$ns"
            return 0
        fi
        phase=$(kubectl get battlegroups -n "$ns" -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)
        printf '    still waiting... (%ds, phase=%s)\n' $((i*10)) "${phase:-Pending}"
        sleep 10
    done
    warn "Not Healthy after ~15 min. It may still come up. Inspect:"
    warn "  kubectl get pods -n ${ns}"
    warn "  kubectl get battlegroups -n ${ns}"
    warn "  less ${LOG}"
    print_summary "$ns"
}

print_summary() {
    local ns="$1"
    cat <<EOF

${C_B}Done.${C_N}
  World:      ${WORLD}
  Region:     $([[ "$REGION" == 1 ]] && echo "Europe" || echo "North America")
  Public IP:  ${PUBLIC_IP}
  Namespace:  ${ns}
  Admin CLI:  sudo -u ${DUNE_USER} ${DUNE_DIR}/bin/battlegroup status

Your server should appear in the in-game Experimental tab within ~2 minutes of Healthy.
Firewall: if you run ufw or a cloud security group, allow UDP 7777-7810 and TCP 31982.
Next: apply the canonical config (docs/02-canonical-config.md).
EOF
}

# ---- main -------------------------------------------------------------------
main() {
    printf '%b\n' "${C_B}Dune: Awakening self-host installer v${VERSION}${C_N}"
    $DRY_RUN && warn "DRY RUN: no changes will be made."
    preflight
    guard_existing_server
    step_create_user
    step_install_steamcmd
    install_rc_shim
    step_install_k3s
    step_containerd_symlink
    step_dune_dirs
    step_settings_conf
    step_pull_payload
    step_run_bootstrap
    step_wait_healthy
}

main "$@"
