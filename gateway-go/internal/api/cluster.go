package api

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/cluster.py: Administration → Cluster, read by administrators and observers. With
// nothing to show the reason («kubernetes» off it, «off» with the chart's clusterView off);
// on Kubernetes what runs now (services/cluster_view.py snapshot, cluster_view.go).
func init() {
	register("cluster", func(d *Deps) []server.Route {
		k := kube.FromEnv()
		answer := deps.Observer(d.Auth, d.DB, d.Log, "/admin/cluster", func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
			if reason := k.ClusterDisabledReason(); reason != "" {
				writePy(w, pyjson.Object{{Key: "disabled_reason", Value: reason}})
				return
			}
			// the route's `except KubeError` never fires: _namespace keeps a KubeError as the
			// namespace's error, and nothing else in snapshot raises one
			snap, err := clusterSnapshot(r.Context(), d)
			if err != nil {
				fail(d, w, "the cluster's snapshot", err)
				return
			}
			writePy(w, append(pyjson.Object{{Key: "disabled_reason", Value: nil}}, snap...))
		})
		return []server.Route{{Method: http.MethodGet, Pattern: "/admin/cluster", Handler: answer}}
	})
}
