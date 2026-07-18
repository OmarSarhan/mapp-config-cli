#!/bin/sh
set -eu

alias_name="config.localhost"
hosts_file="${MAPP_HOSTS_FILE:-/etc/hosts}"
host_ipv4="${MAPP_HOST_IPV4:-}"

if [ -z "${host_ipv4}" ]; then
  host_ipv4="$(
    getent ahostsv4 host.docker.internal 2>/dev/null \
      | awk 'NR == 1 { print $1 }'
  )"
fi

if [ -z "${host_ipv4}" ] && command -v ip >/dev/null 2>&1; then
  host_ipv4="$(ip -4 route show default | awk 'NR == 1 { print $3 }')"
fi

case "${host_ipv4}" in
  ""|*[!0-9.]*)
    printf 'Could not determine the Docker host IPv4 address for %s.\n' \
      "${alias_name}" >&2
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
