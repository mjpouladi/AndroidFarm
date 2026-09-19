FROM nginxinc/nginx-unprivileged:1.28-alpine
COPY images/gateway/nginx.conf /etc/nginx/nginx.conf
USER 101:101
EXPOSE 8080
# Avoid upstream entrypoint scripts attempting to write to the read-only config.
ENTRYPOINT ["nginx", "-g", "daemon off;"]
