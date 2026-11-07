// Package pod is the program of a data API's pods (docs/specs/data-apis.md, «Runtime»): it
// asks the gateway for its manifest at every poll, downloads each resource's parquet through
// the short-lived link the manifest gives, turns it once into a DuckDB file sorted by the
// first filterable column, and answers /data/{api}/{resource} from it, read-only, with the
// keys checked here and every request's parameters bound, never written into the SQL.
package pod

import (
	"errors"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Config is what the pod reads of its environment: the reconcile loop sets it.
type Config struct {
	// DATA_API__ID: the data API this pod answers for
	APIID int64
	// DATA_API__GATEWAY_URL: the gateway's address inside the cluster (its Service)
	GatewayURL string
	// DATA_API__POD_TOKEN: the data API's pod token, from the Deployment's Secret
	PodToken string
	// DATA_API__POLL_SECONDS: the poll until the first manifest says otherwise
	Poll time.Duration
	// DATA_API__DATA_DIR: where the data files go, the pod's emptyDir (the only place it writes)
	DataDir string
	// DATA_API__PORT
	Port int
	// DATA_API__MEMORY_LIMIT_MB: DuckDB's memory, for every resource together and the
	// preparation of the next version: by default 60% of the container's memory limit
	// (cgroup), or 512 MB without one. A third serves (the reads touch few blocks), two
	// thirds prepare (sorting a table wants about 200 MB per thread: Budget)
	MemoryMB int
	// DATA_API__THREADS: DuckDB's threads preparing a version (within the memory: Budget); by
	// default the container's CPUs. Serving, each query has one, and as many queries run at
	// once (the others wait their turn: Server.gate)
	Threads int
	// DATA_API__FORWARDED_ALLOW_IPS: the proxies (the ingress controller) whose
	// X-Forwarded-For is believed, for the keys' address limits; «*» for any
	ForwardedAllowIPs []string
	// DATA_API__QUERY_SECONDS: the longest a request may take
	QueryTimeout time.Duration
	// DATA_API__MANIFEST_MAX_AGE_SECONDS: past this without a manifest the pod stops
	// answering (a revoked key must not live on while the gateway is out of reach)
	ManifestMaxAge time.Duration
	// HOSTNAME: the pod's name, in the usage reports and the logs
	PodName string
}

// LoadConfig reads the environment; the errors name the variables.
func LoadConfig(env func(string) string) (Config, error) {
	var problems []string
	str := func(name, def string) string {
		if v := strings.TrimSpace(env(name)); v != "" {
			return v
		}
		return def
	}
	integer := func(name string, def, lo, hi int) int {
		v := str(name, "")
		if v == "" {
			return def
		}
		n, err := strconv.Atoi(v)
		if err != nil || n < lo || n > hi {
			problems = append(problems, fmt.Sprintf("%s: a whole number from %d to %d", name, lo, hi))
			return def
		}
		return n
	}
	c := Config{
		GatewayURL:     strings.TrimRight(str("DATA_API__GATEWAY_URL", ""), "/"),
		PodToken:       str("DATA_API__POD_TOKEN", ""),
		Poll:           time.Duration(integer("DATA_API__POLL_SECONDS", 20, 1, 3600)) * time.Second,
		DataDir:        str("DATA_API__DATA_DIR", "/data"),
		Port:           integer("DATA_API__PORT", 8000, 1, 65535),
		MemoryMB:       integer("DATA_API__MEMORY_LIMIT_MB", defaultMemoryMB(), 64, 1<<20),
		Threads:        integer("DATA_API__THREADS", defaultThreads(), 1, 256),
		QueryTimeout:   time.Duration(integer("DATA_API__QUERY_SECONDS", 30, 1, 3600)) * time.Second,
		ManifestMaxAge: time.Duration(integer("DATA_API__MANIFEST_MAX_AGE_SECONDS", 900, 30, 86400)) * time.Second,
		PodName:        str("HOSTNAME", "data-api"),
	}
	id, err := strconv.ParseInt(str("DATA_API__ID", ""), 10, 64)
	if err != nil || id < 1 {
		problems = append(problems, "DATA_API__ID: the data API's id is required")
	}
	c.APIID = id
	if c.GatewayURL == "" {
		problems = append(problems, "DATA_API__GATEWAY_URL: the gateway's address is required")
	}
	if c.PodToken == "" {
		problems = append(problems, "DATA_API__POD_TOKEN: the pod token is required")
	}
	for _, p := range strings.Split(str("DATA_API__FORWARDED_ALLOW_IPS", ""), ",") {
		if p = strings.TrimSpace(p); p != "" {
			c.ForwardedAllowIPs = append(c.ForwardedAllowIPs, p)
		}
	}
	if len(problems) > 0 {
		return c, errors.New(strings.Join(problems, "; "))
	}
	return c, nil
}

// defaultMemoryMB is 60% of the container's memory limit: the rest for the program itself,
// the pages being written and what DuckDB holds beyond its limit
func defaultMemoryMB() int {
	if b, err := os.ReadFile("/sys/fs/cgroup/memory.max"); err == nil {
		if n, err := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64); err == nil && n > 0 {
			return max(int(n*6/10>>20), 64)
		}
	}
	return 512
}

// MBPerPrepareThread is about what DuckDB needs per thread to sort a table into its file
// (measured on 20 million rows: 244 MB fail with two threads and pass with one, 400 MB pass
// with two)
const MBPerPrepareThread = 200

// Budget splits the memory: what serves each of `resources`, what prepares, with how many
// threads.
func (c Config) Budget(resources int) (serveMB, prepareMB, prepareThreads int) {
	resources = max(resources, 1)
	serveMB = max(c.MemoryMB/3/resources, 32)
	prepareMB = max(c.MemoryMB*2/3, 64)
	prepareThreads = min(max(c.Threads, 1), max(prepareMB/MBPerPrepareThread, 1))
	return serveMB, prepareMB, prepareThreads
}

// defaultThreads are the container's CPUs (cgroup v2 cpu.max), rounded up
func defaultThreads() int {
	if b, err := os.ReadFile("/sys/fs/cgroup/cpu.max"); err == nil {
		f := strings.Fields(string(b))
		if len(f) == 2 && f[0] != "max" {
			quota, e1 := strconv.ParseFloat(f[0], 64)
			period, e2 := strconv.ParseFloat(f[1], 64)
			if e1 == nil && e2 == nil && period > 0 {
				return max(int((quota+period-1)/period), 1)
			}
		}
	}
	return max(min(runtime.NumCPU(), 4), 1)
}
