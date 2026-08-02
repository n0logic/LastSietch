#!/usr/bin/env bash
# Pre-publish gate for the Last Sietch public repository.
#
# Run BEFORE anything leaves the machine. CI runs this too, but CI runs after
# push, and on a public repo a failing CI gate means the content is already
# public and already in history. The local run is the one that matters.
#
#   ./scripts/preflight.sh              check tracked files (default)
#   ./scripts/preflight.sh <dir>        check a staging directory before copy-in
#
# Exit 0 clean, 1 findings. Every check prints what it looked at, because a
# gate that silently examines nothing reports success.

set -uo pipefail

TARGET="${1:-}"
repo_local_names="$(git rev-parse --show-toplevel 2>/dev/null || echo .)/.preflight-local"
fail=0
note() { printf '  \033[31m[BLOCK]\033[0m %s\n' "$1"; fail=1; }
ok()   { printf '  \033[32m[ok]\033[0m %s\n' "$1"; }

# ---------------------------------------------------------------------------
# Build the candidate file list.
#
# Tracked-file mode is blind to anything gitignored, and in the source monorepo
# the gitignored set is exactly where the credentials live (admin guides, seed
# scripts, .env files, raw Discord exports). So directory mode walks the actual
# filesystem instead, and that is the mode to use when staging a copy-in.
# ---------------------------------------------------------------------------
if [ -n "$TARGET" ]; then
    [ -d "$TARGET" ] || { echo "not a directory: $TARGET" >&2; exit 2; }
    # Prune build artefacts and vendored trees: they are never publish
    # candidates and their volume hides real findings.
    mapfile -t FILES < <(find "$TARGET" \
        \( -name .git -o -name __pycache__ -o -name node_modules \
           -o -name .venv -o -name venv -o -name .svelte-kit -o -name build \
           -o -name dist -o -name .pytest_cache -o -name .ruff_cache \) -prune -o \
        -type f -printf '%P\n')
    echo "preflight: directory mode, ${#FILES[@]} files under $TARGET"
    ROOT="$TARGET"
else
    mapfile -t FILES < <(git ls-files)
    echo "preflight: tracked mode, ${#FILES[@]} files"
    echo "  note: tracked mode cannot see gitignored files. Use directory mode"
    echo "        when staging a copy-in from the private monorepo."
    ROOT="."
fi
[ "${#FILES[@]}" -gt 0 ] || { echo "no files to check" >&2; exit 2; }

# ---------------------------------------------------------------------------
# 1. Path deny-list. Classes that must never be published, on copyright,
#    privacy, or credential grounds. Case-insensitive: the source repo's
#    .git/config sets ignorecase, so case variants are real.
# ---------------------------------------------------------------------------
echo "-- path deny-list --"
DENY=(
  # game-derived assets
  '\.(pak|utoc|ucas|uasset|umap|glb|gltf|mask)$'
  '/glb/' 'static/terrain/' 'layout_[0-9]+\.(bin|json|mask)$'
  'dune-icons/' 'pak-meta'
  # Funcom source material and their own shipped config
  'funcom-procs' '_all\.sql$' 'cvar-catalog/.*\.(json|txt)$'
  'Default(Game|Engine)\.ini$'
  # credentials and key material
  'SERVER-ADMIN-GUIDE' 'GETTING-STARTED\.md$' 'seed_users\.py$'
  'secrets?\.ya?ml$' '-secret\.(ya?ml|json)$'
  '\.(pem|key|p12|pfx|jks)$' 'kubeconfig' 'id_(rsa|ed25519)$'
  'k3s-bootstrap/' '02-phase2-config-files' '05-fls-endpoint-inventory'
  # monetary. NOTE: these paths contain spaces, hence the line-oriented loop
  'keys for raffle/' 'textKeys\.txt$'
  # third-party content and PII
  'DISCORD-INTEL-' 'INTEL-PULL-' 'COMPETITOR-' 'DUNE-COMMUNITY-INTEL'
  'dune-discord-export/' 'community-repos/' 'SECURITY-AUDIT'
  # live production state
  'ops/artifacts/' 'rollback-.*\.json$' 'morning-window-' 'snapshots/'
  # captures
  '\.(pcap|pcapng)$'
  # out of scope
  'volumio' 'jtc-bot/' 'navidrome/' 'scratchpad/'
)
hits=0
for pat in "${DENY[@]}"; do
    for f in "${FILES[@]}"; do
        if printf '%s' "$f" | grep -qiE "$pat"; then
            note "$f  (matched /$pat/)"
            hits=$((hits + 1))
        fi
    done
done
[ "$hits" -eq 0 ] && ok "no denied paths (${#DENY[@]} patterns applied)"

# ---------------------------------------------------------------------------
# 2. Binary allowlist. Inverted on purpose: enumerating forbidden asset types
#    always lags the next one. A .gitignore rule pointed one directory too
#    shallow once tracked 43 game-derived files that every extension check
#    missed. So name what is ALLOWED and fail on the rest.
# ---------------------------------------------------------------------------
echo "-- binary allowlist --"
bin_hits=0
for f in "${FILES[@]}"; do
    p="$ROOT/$f"
    [ -f "$p" ] || continue
    grep -Iq . "$p" 2>/dev/null && continue          # text, fine
    if printf '%s' "$f" | grep -qE '\.(png|svg|ico|woff2?)$' \
       && printf '%s' "$f" | grep -qE '^(docs/img/|\.github/|static/(icons|fonts)/)'; then
        continue
    fi
    note "unexpected binary: $f ($(wc -c < "$p") bytes)"
    bin_hits=$((bin_hits + 1))
done
[ "$bin_hits" -eq 0 ] && ok "no unexpected binaries"

# ---------------------------------------------------------------------------
# 3. Content scan. Paths alone are not enough: an innocuously named CR dump
#    carried two classes of live credential and would pass any name-based
#    review. This looks inside.
# ---------------------------------------------------------------------------
echo "-- content scan --"
declare -a CONTENT=(
  'Func[A-Za-z0-9]{30,}:live Funcom AuthToken'
  # NOTE: the string "FuncomLiveServices__ServiceAuthToken" is a KEY NAME inside
  # a Kubernetes secret, not a token. Code that reads the secret at runtime has
  # to name the key, so flagging it blocks correct work. The token VALUE is
  # covered by the Func-prefixed rule above, which is what actually matters.
  'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.:JWT'
  'funcom-seabass-sh-[0-9a-f]{16}:real namespace with HostId'
  '\bsh-[0-9a-f]{16}-:real battlegroup id'
  '(^|[^0-9])1[0-9]{17,18}([^0-9]|$):Discord snowflake'
  # An install prefix like /opt/something is NOT a disclosure: it is a
  # directory name, it reveals nothing about anyone's infrastructure, and the
  # scripts here already carry it correctly as ${VAR:-/opt/default}. Flagging
  # it fired on properly templated code, which is how a gate gets switched
  # off. Install paths are documented as a convention instead.
  'discord(app)?\.com/api/webhooks/:live webhook'
  'xox[baprs]-|ghp_[A-Za-z0-9]{20,}:third-party token'
  '[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}:email address'
  # Internal operational narrative. Not secret, but it is ours rather than the
  # reader's: our incident dates, our hosting provider, our private notes, our
  # ticket numbers. It makes a public repo read as somebody's leaked runbook.
  '(feedback|reference|project)_[a-z]+_[a-z_]{4,}:private knowledge-base reference'
  # Wiki links to our private notes look like [[feedback_dune_thing]] or
  # [[reference-dune-thing]]: always lowercase and always containing an
  # underscore or hyphen. Requiring that separator keeps this off TOML
  # array-of-tables headers such as [[allowlists]], and off bash [[ ]] tests,
  # which contain spaces. A blanket \[\[ pattern once ate every bash test
  # expression in a scrub and left "if; then" behind, so this one stays narrow.
  '\[\[[a-z][a-z0-9]*[_-][a-z0-9_-]*\]\]:wiki-style link to a private note'
  'ops/maint-[0-9]{4}-[0-9]{2}:reference to an unpublished runbook'
  '(nobody was watching|a human noticed|hit us twice|bit us twice):incident narrative, generalise it'
)

# Deployment specifics cannot be listed here. This file is PUBLIC, so an IP
# address, host alias, or player name written into it would be exactly the
# disclosure the gate exists to prevent: the rule would leak the thing it
# protects. A generic "any IPv4" rule is not the answer either, since it fires
# on every game version string like 1.4.10.4, and a gate that cries wolf gets
# switched off.
#
# So the specifics live in an untracked local file, one "regex:label" per
# line, picked up automatically:
#
#   echo '203\.0\.113\.7:my game host IP' >> .preflight-local
#   echo 'myhandle:my player handle'      >> .preflight-local
#
# .preflight-local is gitignored. Every operator populates it with their own
# addresses, aliases, and community names.
if [ -f "$repo_local_names" ]; then
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        case "$line" in \#*) continue ;; esac
        CONTENT+=("$line")
    done < "$repo_local_names"
fi
c_hits=0
for entry in "${CONTENT[@]}"; do
    pat="${entry%%:*}"; label="${entry#*:}"
    for f in "${FILES[@]}"; do
        # This file DEFINES the patterns, so it matches them by construction.
        # Excluded from the content scan only; the path and binary gates above
        # still cover it. Announced below rather than skipped silently, since
        # an unannounced exclusion is a place to hide something.
        case "$f" in */preflight.sh|preflight.sh) continue ;; esac
        p="$ROOT/$f"
        [ -f "$p" ] || continue
        grep -Iq . "$p" 2>/dev/null || continue      # skip binaries
        if grep -qEi "$pat" "$p" 2>/dev/null; then
            n=$(grep -cEi "$pat" "$p" 2>/dev/null)
            note "$label in $f ($n occurrence(s))"
            c_hits=$((c_hits + 1))
        fi
    done
done
[ "$c_hits" -eq 0 ] && ok "no flagged content (${#CONTENT[@]} patterns applied)"
echo "     (scripts/preflight.sh itself is excluded from the content scan:"
echo "      it defines these patterns. Review it by eye when it changes.)"

echo
if [ "$fail" -ne 0 ]; then
    cat <<'EOF'
PREFLIGHT FAILED. Nothing should be pushed.

Placeholders are the correct output, not a violation. A namespace written as
funcom-seabass-sh-<your-hostid>-<random> passes; one carrying a literal
16-hex host id does not.

If a finding is a false positive, widen the rule in this file in the same
commit so the decision is reviewable, rather than skipping the gate.
EOF
    exit 1
fi
echo "PREFLIGHT CLEAN."
