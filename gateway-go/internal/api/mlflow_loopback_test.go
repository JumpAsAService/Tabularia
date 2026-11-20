package api

import (
	"net/netip"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/egress"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
)

// The tests of this package serve MLflow from a local port: the loopback is allowed to the
// connection's client here, nothing else (the rule is tested in mlflowconn).
func init() {
	mlflowconn.ForbiddenAddress = func(a netip.Addr) bool { return !a.Unmap().IsLoopback() && egress.Forbidden(a) }
}
