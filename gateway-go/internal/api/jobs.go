package api

import (
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
)

// The background loops (app/main.py start_background_work) register here as the routes do,
// one file each, so that ports done side by side never edit a shared list. A loop that
// needs this package's helpers (a run's reconcile, the webhooks' enqueue) lives in it; one
// that does not may live in a package of its own and call RegisterJob from there.
var jobBuilders = map[string]func(*Deps) jobs.Job{}

// RegisterJob adds a loop; `name` is its name in start_background_work.
func RegisterJob(name string, build func(*Deps) jobs.Job) {
	if _, dup := jobBuilders[name]; dup {
		panic("api: job registered twice: " + name)
	}
	jobBuilders[name] = build
}

// Jobs is every background loop, in start_background_work's order.
func Jobs(d *Deps) []jobs.Job {
	kubePanel.Store(d.Panel)
	var out []jobs.Job
	for name, build := range jobBuilders {
		j := build(d)
		j.Name = name
		out = append(out, j)
	}
	return jobs.Sorted(out)
}
