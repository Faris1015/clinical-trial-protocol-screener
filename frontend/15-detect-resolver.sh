#!/bin/sh
# Point nginx's resolver at the container's own DNS server, written to an include
# the server block pulls in. Paired with the variable proxy_pass in
# default.conf.template, this makes the /api upstream resolve *per request*
# instead of once at startup — so the frontend boots and serves the SPA even
# when the backend is momentarily unresolvable (API calls just 502 until it's
# back) rather than nginx refusing to start.
#
# Reads the nameserver from resolv.conf so it works unchanged across topologies:
# Docker's embedded DNS (127.0.0.11) under compose, Fly's 6PN resolver on Fly.
# Runs from /docker-entrypoint.d before nginx starts (the `15-` prefix orders it
# ahead of the envsubst step, though any order works — nginx starts last).
set -eu

ns="$(awk '/^nameserver/ { print $2; exit }' /etc/resolv.conf 2>/dev/null || true)"
: "${ns:=127.0.0.11}"

# valid=10s: re-resolve every 10s so a backend that moves (new Fly machine IP)
# is picked up without a frontend restart.
printf 'resolver %s valid=10s;\n' "$ns" > /etc/nginx/resolver.conf
echo "15-detect-resolver.sh: nginx resolver set to $ns"