// jobsonce runs ONE round of a background loop (internal/jobs), at a fixed moment: what the
// comparison («a un giro», scripts/diffcheck.sh) runs on the Go side, while its Python side
// runs the same round of the Python service (cmd/diffcheck/ticks/ticks.py).
//
//	jobsonce list                          the rounds it knows
//	jobsonce <round> [-now 2026-03-29T00:59:00Z]   one round, its result as JSON
//	jobsonce serve [-listen :8000]         POST /tick {"tick": …, "now": …} → {"result": …} | {"error": …}
//
// It reads the same environment as the gateway; the panel's settings are read again before
// every round, as the Python side does.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/db"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	_ "github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs/all"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: jobsonce list | jobsonce <round> [-now RFC3339] | jobsonce serve [-listen :8000]")
		os.Exit(2)
	}
	ctx := context.Background()
	if os.Args[1] == "list" {
		r, err := setup(ctx, log)
		if err != nil {
			fail(err)
		}
		names := make([]string, 0, len(r.ticks))
		for n := range r.ticks {
			names = append(names, n)
		}
		sort.Strings(names)
		for _, n := range names {
			fmt.Println(n)
		}
		return
	}
	if os.Args[1] == "serve" {
		fs := flag.NewFlagSet("serve", flag.ExitOnError)
		listen := fs.String("listen", ":8000", "where to listen")
		_ = fs.Parse(os.Args[2:])
		r, err := setup(ctx, log)
		if err != nil {
			fail(err)
		}
		log.Info("jobsonce listening", "addr", *listen)
		fail(http.ListenAndServe(*listen, r))
	}
	fs := flag.NewFlagSet("tick", flag.ExitOnError)
	now := fs.String("now", "", "the moment of the round (RFC 3339); now when empty")
	_ = fs.Parse(os.Args[2:])
	r, err := setup(ctx, log)
	if err != nil {
		fail(err)
	}
	out, err := r.run(ctx, os.Args[1], *now, nil)
	if err != nil {
		fail(err)
	}
	b, _ := json.Marshal(out)
	fmt.Println(string(b))
}

type runner struct {
	mu    sync.Mutex // one round at a time, as the comparison sends them
	log   *slog.Logger
	pool  *pgxpool.Pool
	env   string // the overlay the jobs were built with (JSON), see build
	d     *api.Deps
	ticks map[string]jobs.Tick
}

func setup(ctx context.Context, log *slog.Logger) (*runner, error) {
	cfg, err := load()
	if err != nil {
		return nil, err
	}
	pool, err := db.Open(ctx, cfg.DSN, 0)
	if err != nil {
		return nil, err
	}
	if err := db.CheckSchema(ctx, pool); err != nil {
		return nil, err
	}
	r := &runner{log: log, pool: pool}
	return r, r.build(ctx, nil)
}

// load reads the environment. The loops do not depend on the role: it is «orchestrator»,
// which needs no upstream.
func load() (*config.Config, error) {
	osEnv := config.FromOS()
	return config.Load(func(name string) (string, bool) {
		if strings.EqualFold(name, "APP__ROLE") {
			return "orchestrator", true
		}
		return osEnv(name)
	})
}

// original is the process's environment at start; a round's overlay goes on top of it, in
// the process's own environment (some packages read a variable at use, as Python does)
var original = os.Environ()

func setEnv(overlay map[string]string) {
	os.Clearenv()
	for _, kv := range original {
		k, v, _ := strings.Cut(kv, "=")
		_ = os.Setenv(k, v)
	}
	for k, v := range overlay {
		_ = os.Setenv(k, v)
	}
}

// build makes the jobs with the environment plus `overlay` (a round's own variables: an
// installation where a feature is on). The same overlay as the last round keeps the jobs,
// and whatever state they hold between rounds, as one Python process keeps its modules'.
func (r *runner) build(ctx context.Context, overlay map[string]string) error {
	key, _ := json.Marshal(overlay)
	if r.d != nil && string(key) == r.env {
		return nil
	}
	setEnv(overlay)
	cfg, err := load()
	if err != nil {
		return err
	}
	panel := settings.New(cfg, cfg.SettingsPanel, r.log)
	panel.Reload(ctx, r.pool)
	engineClient, err := engine.New(cfg.EngineBaseURL, cfg.FernetKey)
	if err != nil {
		return err
	}
	fernet, err := secret.NewFernet(cfg.FernetKey)
	if err != nil {
		return err
	}
	authn := &auth.Authenticator{DB: r.pool, Secret: cfg.JWTSecret, Algorithm: cfg.JWTAlgorithm, CookieDomain: cfg.CookieDomain,
		Settings: panel, ClientIP: clientip.New(cfg.ForwardedAllowIPs), Log: r.log}
	d := &api.Deps{DB: r.pool, Auth: authn, Panel: panel, Config: cfg, Log: r.log, Engine: engineClient, Fernet: fernet}
	ticks := map[string]jobs.Tick{}
	for _, j := range api.Jobs(d) {
		for name, t := range j.Ticks {
			if _, dup := ticks[name]; dup {
				return fmt.Errorf("round %q declared twice", name)
			}
			ticks[name] = t
		}
	}
	r.d, r.ticks, r.env = d, ticks, string(key)
	return nil
}

func (r *runner) run(ctx context.Context, name, now string, overlay map[string]string) (any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.build(ctx, overlay); err != nil {
		return nil, err
	}
	t, ok := r.ticks[name]
	if !ok {
		return nil, fmt.Errorf("no round %q (jobsonce list)", name)
	}
	if now != "" {
		at, err := time.Parse(time.RFC3339Nano, now)
		if err != nil {
			return nil, fmt.Errorf("-now: %v", err)
		}
		jobs.Freeze(&at)
		defer jobs.Freeze(nil)
	}
	r.d.Panel.Reload(ctx, r.d.DB)
	return t(ctx)
}

// ServeHTTP is POST /tick.
func (r *runner) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost || req.URL.Path != "/tick" {
		http.NotFound(w, req)
		return
	}
	var in struct {
		Tick string            `json:"tick"`
		Now  string            `json:"now"`
		Env  map[string]string `json:"env"`
	}
	if err := json.NewDecoder(req.Body).Decode(&in); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	out, err := r.run(req.Context(), in.Tick, in.Now, in.Env)
	w.Header().Set("Content-Type", "application/json")
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(map[string]any{"error": err.Error()})
		return
	}
	_ = json.NewEncoder(w).Encode(map[string]any{"result": out})
}

func fail(err error) {
	if err == nil {
		err = errors.New("stopped")
	}
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
