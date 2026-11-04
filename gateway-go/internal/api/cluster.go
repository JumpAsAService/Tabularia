package api

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("cluster", func(d *Deps) []server.Route {
		k := kube.FromEnv()
		answer := deps.Observer(d.Auth, d.DB, d.Log, "/admin/cluster", func(w http.ResponseWriter, _ *http.Request, _ *auth.Principal) {
			writePy(w, pyjson.Object{{Key: "disabled_reason", Value: k.ClusterDisabledReason()}})
		})
		return []server.Route{{Method: http.MethodGet, Pattern: "/admin/cluster", Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// routes/cluster.py cluster: with nothing to show, the reason; the snapshot of the
			// pods (services/cluster_view.py, the Kubernetes API) is still Python's
			if k.ClusterDisabledReason() == "" {
				d.Upstream.ServeHTTP(w, r)
				return
			}
			answer.ServeHTTP(w, r)
		})}}
	})
}
