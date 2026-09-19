#!/bin/sh
set -eu
: "${CONTROL_CIDR:?CONTROL_CIDR is required}"
: "${EGRESS_CIDR:?EGRESS_CIDR is required}"
# Install a closed policy BEFORE parsing secrets or launching the tunnel.
iptables -P OUTPUT DROP
iptables -P INPUT DROP
iptables -P FORWARD DROP
ip6tables -P OUTPUT DROP
ip6tables -P INPUT DROP
ip6tables -P FORWARD DROP
rm -f /run/direct
python3 /configure.py
if [ -f /run/direct ]; then
    # Direct host egress: the mounted secret selected {"type": "direct"}. The
    # sidecar still owns the shared namespace, the loopback-only ADB port and
    # the ingress policy; only the transparent tunnel is absent. The host guard
    # chain (ops/farmctl.py) remains the authority for holds and stops.
    iptables -F OUTPUT
    iptables -F INPUT
    iptables -P OUTPUT ACCEPT
    iptables -A INPUT -i lo -j ACCEPT
    iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A INPUT -p tcp -s "$CONTROL_CIDR" --dport 5555 -j ACCEPT
    iptables -A INPUT -p tcp -s "$EGRESS_CIDR" --dport 5555 -j ACCEPT
    exec python3 -c 'import signal, sys; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); signal.pause()'
fi
UPSTREAM_IP=$(cat /run/upstream-ip)
UPSTREAM_PORT=$(cat /run/upstream-port)
iptables -F OUTPUT
iptables -F INPUT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -p tcp -d "$UPSTREAM_IP" --dport "$UPSTREAM_PORT" -m owner --uid-owner 900 -j ACCEPT
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -p tcp -s "$CONTROL_CIDR" --dport 5555 -j ACCEPT
# Docker's loopback-only published ADB port is DNATed from this private bridge.
iptables -A INPUT -p tcp -s "$EGRESS_CIDR" --dport 5555 -j ACCEPT
# Insert before Docker's embedded DNS NAT so DNS cannot escape through 127.0.0.11.
iptables -t nat -N FARM_OUT 2>/dev/null || true
iptables -t nat -F FARM_OUT
iptables -t nat -C OUTPUT -j FARM_OUT 2>/dev/null || iptables -t nat -I OUTPUT 1 -j FARM_OUT
iptables -t nat -A FARM_OUT -m owner --uid-owner 900 -j RETURN
iptables -t nat -A FARM_OUT -p udp --dport 53 -j REDIRECT --to-ports 1053
iptables -t nat -A FARM_OUT -p tcp --dport 53 -j REDIRECT --to-ports 1053
iptables -t nat -A FARM_OUT -d 127.0.0.0/8 -j RETURN
iptables -t nat -A FARM_OUT -p tcp -j REDIRECT --to-ports 12345
sing-box check -c /run/sing-box.json
exec setpriv --reuid=900 --regid=900 --clear-groups sing-box run -c /run/sing-box.json
