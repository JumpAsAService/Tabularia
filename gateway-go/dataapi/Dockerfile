# The data APIs' pods (docs/specs/data-apis.md, «Runtime»): a Go program with DuckDB linked in,
# so cgo and a build stage of its own; the gateway's image stays without. Built from the
# repository root's gateway-go/ (the program shares the gateway's internal/dataapiwire and
# internal/clientip through its go.mod's replace):
#   docker build -t tabularia-data-api:dev -f gateway-go/dataapi/Dockerfile gateway-go
FROM golang:1.27.3-bookworm AS build
# the release's version (the release workflow gives it), printed by `/dataapi version`
ARG VERSION=dev
WORKDIR /src
COPY go.mod go.sum ./
COPY internal/dataapiwire internal/dataapiwire
COPY internal/clientip internal/clientip
COPY dataapi dataapi
WORKDIR /src/dataapi
# `go build` fetches only the modules this platform links (`go mod download` would fetch
# DuckDB's libraries of every platform); then the licences of each of them, checked
RUN CGO_ENABLED=1 go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /out/dataapi . \
 && sh licences.sh /out/licenses

FROM debian:bookworm-slim
# the CA certificates, for the storage's links over TLS
RUN set -e; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --system --gid 10001 app; \
    useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app; \
    mkdir -p /data && chown 10001:10001 /data && chmod 700 /data
COPY --from=build /out/dataapi /dataapi
COPY --from=build /out/licenses /usr/share/licenses/tabularia-data-api
USER 10001:10001
# the only place it writes: an emptyDir in the pod (the root filesystem read-only)
VOLUME /data
ENV DATA_API__DATA_DIR=/data
EXPOSE 8000
ENTRYPOINT ["/dataapi"]
