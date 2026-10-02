#!/usr/bin/env bash
#
# Make the HTTPS certificate the API and MCP servers serve with ([tls] in settings),
# signed by a small lab CA kept beside it.
#
# Usage:
#   scripts/make_lab_cert.sh [--ip ADDR]... [--dns NAME]... [--days N] [--out DIR]
#
#   --ip    an address clients connect to (repeatable). Default: this host's first
#           address from `hostname -I`. 127.0.0.1 is always included.
#   --dns   a hostname clients connect to (repeatable). `localhost` and this host's
#           name are always included.
#   --days  server certificate lifetime (default 365). Rerun before it runs out;
#           start_server_host.sh warns 30 days ahead.
#   --out   where the files go (default cfg/tls, git-ignored)
#
# The CA is made once (ca.crt / ca.key, valid 10 years) and reused on every rerun, so
# clients trust it once and keep trusting renewed server certificates. Give clients
# ca.crt -- never ca.key or server.key:
#
#   browser / OS   import ca.crt as a trusted root (for the sign-in page and Lumi-Deck)
#   Claude Code    export NODE_EXTRA_CA_CERTS=/path/to/ca.crt  (Node ignores the OS store)
#   Python         SSL_CERT_FILE or REQUESTS_CA_BUNDLE pointed at a bundle including it
#
# Every client must connect by a name or address listed in the certificate; anything
# else fails the TLS check. Rerun with the extra --ip/--dns to add one.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

IPS=()
DNS_NAMES=()
DAYS=365
OUT="cfg/tls"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip) IPS+=("$2"); shift 2 ;;
        --dns) DNS_NAMES+=("$2"); shift 2 ;;
        --days) DAYS="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        -h|--help) sed -n '3,27p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

command -v openssl >/dev/null || { echo "error: openssl not found" >&2; exit 1; }

if [[ ${#IPS[@]} -eq 0 ]]; then
    FIRST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [[ -n "$FIRST_IP" ]] && IPS+=("$FIRST_IP")
fi
IPS+=("127.0.0.1")
DNS_NAMES+=("localhost" "$(hostname)")

# subjectAltName, de-duplicated, in the order given.
SAN=""
declare -A SEEN=()
for ip in "${IPS[@]}"; do
    [[ -n "${SEEN[IP:$ip]:-}" ]] && continue; SEEN[IP:$ip]=1; SAN+="IP:$ip,"
done
for name in "${DNS_NAMES[@]}"; do
    [[ -n "${SEEN[DNS:$name]:-}" ]] && continue; SEEN[DNS:$name]=1; SAN+="DNS:$name,"
done
SAN="${SAN%,}"

umask 077
mkdir -p "$OUT"

if [[ -f "$OUT/ca.crt" && -f "$OUT/ca.key" ]]; then
    echo "reusing the lab CA in $OUT (clients that trust it keep trusting the new cert)"
else
    echo "making a lab CA in $OUT ..."
    openssl req -x509 -new -newkey rsa:4096 -nodes -sha256 -days 3650 \
        -keyout "$OUT/ca.key" -out "$OUT/ca.crt" \
        -subj "/O=Lumi-Lab/CN=Lumi-Lab CA ($(hostname))" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
fi

echo "making the server certificate for $SAN ..."
openssl req -new -newkey rsa:2048 -nodes -sha256 \
    -keyout "$OUT/server.key" -out "$OUT/server.csr" \
    -subj "/O=Lumi-Lab/CN=$(hostname)" 2>/dev/null
openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" \
    -CAcreateserial -out "$OUT/server.crt" -days "$DAYS" -sha256 \
    -extfile <(printf '%s\n' \
        "subjectAltName=$SAN" \
        "basicConstraints=critical,CA:FALSE" \
        "keyUsage=critical,digitalSignature,keyEncipherment" \
        "extendedKeyUsage=serverAuth") 2>/dev/null
rm -f "$OUT/server.csr"
# The CA certificate is public; clients need a copy.
chmod 644 "$OUT/ca.crt" "$OUT/server.crt"

openssl verify -CAfile "$OUT/ca.crt" "$OUT/server.crt" >/dev/null
echo
echo "  $OUT/server.crt  $(openssl x509 -in "$OUT/server.crt" -noout -enddate)"
echo "  $OUT/server.key  (private, mode 600)"
echo "  $OUT/ca.crt      give this to clients"
echo "  $OUT/ca.key      (private, mode 600) -- keep it; renewals reuse it"
echo
echo "turn HTTPS on in cfg/.secrets.toml, then restart the API and MCP servers:"
echo "  [tls]"
echo "  enabled = true"
