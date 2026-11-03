# The Go gateway (docs/specs/gateway-go.md). Built from the repository root's gateway-go/:
#   docker build -t tabularia-gateway-go:dev gateway-go
# PROGRAM=fakeengine builds the comparison's fake engine instead (scripts/diffcheck.sh).
FROM golang:1.27.3-bookworm AS build
ARG PROGRAM=gateway
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/gateway ./cmd/${PROGRAM}

# no shell, no package manager: the binary and the CA certificates
FROM gcr.io/distroless/static-debian12
COPY --from=build /out/gateway /gateway
# the same unprivileged user as the other images
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/gateway"]
