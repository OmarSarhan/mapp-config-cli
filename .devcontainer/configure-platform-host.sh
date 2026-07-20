#!/bin/sh
set -eu

alias_name="config.localhost"
hosts_file="${MAPP_HOSTS_FILE:-/etc/hosts}"
host_ipv4="${MAPP_HOST_IPV4:-}"
platform_url="${MAPP_PLATFORM_URL:-http://config.localhost:3000}"

if [ -z "${host_ipv4}" ]; then
  gateway_ipv4=""
  docker_host_ipv4=""
  if command -v ip >/dev/null 2>&1; then
    gateway_ipv4="$(ip -4 route show default | awk 'NR == 1 { print $3 }')"
  fi
  docker_host_ipv4="$(
    getent ahostsv4 host.docker.internal 2>/dev/null \
      | awk 'NR == 1 { print $1 }'
  )"
  host_ipv4="$(
    python3 - "${platform_url}" "${gateway_ipv4}" "${docker_host_ipv4}" <<'PY'
import socket
import sys
import urllib.parse

parsed = urllib.parse.urlsplit(sys.argv[1])
port = parsed.port or (443 if parsed.scheme == "https" else 80)
seen = set()
for candidate in sys.argv[2:]:
    if not candidate or candidate in seen:
        continue
    seen.add(candidate)
    try:
        with socket.create_connection((candidate, port), timeout=1.5):
            print(candidate)
            break
    except OSError:
        pass
PY
  )"
fi

case "${host_ipv4}" in
  ""|*[!0-9.]*)
    printf 'Could not reach %s through the Docker gateway or host.docker.internal.\n' \
      "${platform_url}" >&2
    printf 'Start the platform first, or set MAPP_HOST_IPV4 to its reachable host address.\n' >&2
    exit 1
    ;;
esac

temporary="$(mktemp)"
trap 'rm -f "${temporary}"' EXIT HUP INT TERM

awk -v alias_name="${alias_name}" '
  {
    for (field = 2; field <= NF; field += 1) {
      if ($field == alias_name) {
        next
      }
    }
    print
  }
' "${hosts_file}" >"${temporary}"
printf '%s\t%s\n' "${host_ipv4}" "${alias_name}" >>"${temporary}"

# /etc/hosts is a Docker-managed mount, so replace its contents without
# renaming the file.
cat "${temporary}" >"${hosts_file}"
printf 'Configured %s as %s for %s.\n' \
  "${alias_name}" "${host_ipv4}" "${platform_url}"
