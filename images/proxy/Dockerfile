FROM ghcr.io/sagernet/sing-box:v1.12.0 AS binary
FROM alpine:3.21
RUN apk add --no-cache ca-certificates iptables iproute2 python3 curl util-linux
COPY --from=binary /usr/local/bin/sing-box /usr/local/bin/sing-box
COPY images/proxy/configure.py /configure.py
COPY images/proxy/entrypoint.sh /entrypoint.sh
COPY images/proxy/healthcheck.sh /healthcheck.sh
RUN chmod 755 /entrypoint.sh /healthcheck.sh
ENTRYPOINT ["/entrypoint.sh"]
