package main

// routes/cluster.py: Administration → Cluster, off Kubernetes (Docker).
func init() {
	register(Suite{
		Module: "cluster",
		Seed:   seedData,
		Steps: func(w *World) []Step {
			s := forEveryone(w, "the cluster", "GET", "/admin/cluster", nil)
			return append(s, Step{Name: "with a query", Method: "GET", Path: "/admin/cluster?x=1", Auth: w.As("admin")})
		},
	})
}
