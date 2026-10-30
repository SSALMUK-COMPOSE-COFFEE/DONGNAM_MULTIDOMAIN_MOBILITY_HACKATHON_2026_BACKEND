#!/usr/bin/env bash
set -euo pipefail

FRONT_PORT="${FRONT_PORT:-18104}"
API_PORT="${API_PORT:-18103}"
UPSTREAMS="${NGINX_UPSTREAMS_CONF:-/etc/nginx/conf.d/upstreams.conf}"
SITE="${NGINX_SSALHUB_SITE:-/etc/nginx/sites-enabled/wildcard.ssalhub.xyz}"

export HOST="freshswitch.ssalhub.xyz"
export OLD_HOST="freshswitch-api.ssalhub.xyz"
export API_PATTERN='~^freshswitch\.ssalhub\.xyz/api(/|$)'
export FRONT_PORT API_PORT

for port in "$FRONT_PORT" "$API_PORT"; do
    if [[ ! "$port" =~ ^[0-9]+$ ]]; then
        echo "invalid port: $port" >&2
        exit 1
    fi
done

for file in "$UPSTREAMS" "$SITE"; do
    if [[ ! -r "$file" ]]; then
        echo "cannot read $file" >&2
        exit 1
    fi
done

OWNER="$(awk '
    /^[[:space:]]*map[[:space:]]+\$host[[:space:]]+\$upstream_port[[:space:]]*\{/ { inside = 1; next }
    inside && /^[[:space:]]*\}/ { exit }
    inside && NF >= 2 && $1 != "default" && $1 != ENVIRON["HOST"] && $1 != ENVIRON["OLD_HOST"] {
        port = $2; gsub(/;/, "", port)
        if (port == ENVIRON["FRONT_PORT"]) { print $1; exit }
    }
' "$UPSTREAMS")"
if [[ -n "$OWNER" ]]; then
    echo "port $FRONT_PORT is already used by $OWNER" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

awk '
    /^[[:space:]]*map[[:space:]]+\$host[[:space:]]+\$upstream_port[[:space:]]*\{/ { map = "host" }
    /^[[:space:]]*map[[:space:]]+\$host\$uri[[:space:]]+\$upstream_port_path[[:space:]]*\{/ { map = "path" }
    map == "host" && $1 == ENVIRON["OLD_HOST"] { next }
    map == "host" && $1 == ENVIRON["HOST"] { has_host = 1 }
    map == "path" && $1 == ENVIRON["API_PATTERN"] { has_path = 1 }
    map == "host" && !has_host && ($1 == "default" || /^[[:space:]]*\}/) {
        printf "    %-22s %s;\n", ENVIRON["HOST"], ENVIRON["FRONT_PORT"]
        has_host = 1
    }
    map == "path" && !has_path && ($1 == "default" || /^[[:space:]]*\}/) {
        printf "    %s %s;\n", ENVIRON["API_PATTERN"], ENVIRON["API_PORT"]
        has_path = 1
    }
    map != "" && /^[[:space:]]*\}/ { map = "" }
    { print }
    END { if (!has_host || !has_path) exit 1 }
' "$UPSTREAMS" > "$WORK/upstreams"

awk '
    /^[[:space:]]*server[[:space:]]*\{/ { target = 0 }
    /^[[:space:]]*server_name[[:space:]]+\*\.ssalhub\.xyz;[[:space:]]*$/ { target = 1 }
    target && /^[[:space:]]*proxy_pass[[:space:]]+http:\/\/127\.0\.0\.1:\$upstream_port;[[:space:]]*$/ {
        sub(/\$upstream_port;/, "$upstream_port_path;")
        changed = 1
    }
    target && /\$upstream_port_path;/ { changed = 1 }
    { print }
    END { if (!changed) exit 1 }
' "$SITE" > "$WORK/site"

if cmp -s "$UPSTREAMS" "$WORK/upstreams" && cmp -s "$SITE" "$WORK/site"; then
    echo "nginx already configured for $HOST, skipping"
    exit 0
fi

cp "$UPSTREAMS" "$WORK/upstreams.bak"
cp "$SITE" "$WORK/site.bak"

restore() {
    sudo tee "$UPSTREAMS" < "$WORK/upstreams.bak" > /dev/null
    sudo tee "$SITE" < "$WORK/site.bak" > /dev/null
}

if ! sudo tee "$UPSTREAMS" < "$WORK/upstreams" > /dev/null || ! sudo tee "$SITE" < "$WORK/site" > /dev/null; then
    restore
    echo "write failed, restored $UPSTREAMS and $SITE" >&2
    exit 1
fi

if ! sudo nginx -t; then
    restore
    echo "nginx -t failed, restored $UPSTREAMS and $SITE" >&2
    exit 1
fi

sudo systemctl reload nginx
echo "mapped $HOST -> 127.0.0.1:$FRONT_PORT, $HOST/api -> 127.0.0.1:$API_PORT"
