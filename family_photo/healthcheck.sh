#!/bin/bash
# Compose bounds the entire check with timeout(1); no Python startup is needed.
set -euo pipefail

port="${1:-8090}"
exec 3<>"/dev/tcp/127.0.0.1/$port" || exit 1
printf 'GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3 || exit 1
IFS= read -r status <&3 || exit 1
[[ "$status" == 'HTTP/1.1 200 '* || "$status" == 'HTTP/1.0 200 '* ]] || exit 1

# A status line alone is insufficient: require complete HTTP headers as well.
while IFS= read -r header <&3; do
    if [[ "$header" == $'\r' || -z "$header" ]]; then
        exit 0
    fi
done
exit 1
