// The Go gateway (docs/specs/gateway-go.md): answers every route of the Python gateway, as
// FastAPI would; the assistant's routes and the SQL parsing are the AI service's.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/db"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	_ "github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs/all"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/openlineage"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/proxy"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(log); err != nil {
		log.Error("the gateway stopped", "err", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	cfg, err := config.Load(config.FromOS())
	if err != nil {
		return err
	}
	if cfg.AIService == nil {
		return errors.New("configuration: GATEWAY_GO__AI_SERVICE is required: the AI service's address (the gateway's image with APP__ROLE=ai)")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	pool, err := db.Open(ctx, cfg.DSN, 0)
	if err != nil {
		return err
	}
	defer pool.Close()
	if err := db.CheckSchema(ctx, pool); err != nil {
		return err
	}

	panel := settings.New(cfg, cfg.SettingsPanel, log)
	panel.Reload(ctx, pool)
	panel.RememberStart() // overrides.load_at_start → remember_start
	go panel.Loop(ctx, pool)

	authn := &auth.Authenticator{
		DB: pool, Secret: cfg.JWTSecret, Algorithm: cfg.JWTAlgorithm, CookieDomain: cfg.CookieDomain,
		Settings: panel, ClientIP: clientip.New(cfg.ForwardedAllowIPs), Log: log,
	}
	engineClient, err := engine.New(cfg.EngineBaseURL, cfg.FernetKey)
	if err != nil {
		return err
	}
	fernet, err := secret.NewFernet(cfg.FernetKey)
	if err != nil {
		return err
	}
	// the loops need the AI service too (a SQL script on the catalog): every role has it
	deps := &api.Deps{DB: pool, Auth: authn, Panel: panel, Config: cfg, Log: log, Engine: engineClient, Fernet: fernet,
		AIService: proxy.New(cfg.AIService, log), SQL: aisvc.New(cfg.AIService, cfg.FernetKey)}
	// services/openlineage.py: its queue, emptied at the stop (main.py's lifespan, wait_idle 5 s)
	deps.Lineage = openlineage.New(openlineage.FromEnv(), pool, deps.SQL, panel.OpenLineageTimeout, log)
	log.Info(deps.Lineage.Describe())
	defer deps.Lineage.WaitIdle(5 * time.Second)
	// APP__ROLE (app/main.py): «orchestrator» declares no API route, «api» starts no
	// background work, «all» does both
	var handler http.Handler
	if cfg.Upstream != nil {
		log.Warn("GATEWAY_GO__UPSTREAM is set and ignored: the Go gateway answers every route, nothing goes to the Python gateway",
			"upstream", cfg.Upstream.String())
	}
	if cfg.Jobs.Answers() {
		routes := append([]server.Route{{Method: http.MethodGet, Pattern: "/health", Handler: server.Health()}},
			api.Routes(deps)...)
		// the notebooks' own host (NOTEBOOKS__ORIGIN) reaches the notebooks' proxy and nothing else
		handler = api.WithNotebooksHost(deps, server.New(routes, api.ServerOptions(deps, server.MainApp)))
	} else {
		handler = server.Orchestrator(cfg.AppName)
	}
	// start_background_work: the loops stop after the server, as the lifespan's tasks are
	// cancelled once uvicorn has drained its connections
	jobsCtx, stopJobs := context.WithCancel(context.Background())
	defer stopJobs()
	waitJobs := func() {}
	if cfg.Jobs.Works() {
		waitJobs = jobs.Start(jobsCtx, api.Jobs(deps))
	}
	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           handler,
		ReadHeaderTimeout: 30 * time.Second,
		// as uvicorn's --timeout-keep-alive 130 in the production compose
		IdleTimeout: 130 * time.Second,
	}
	errc := make(chan error, 1)
	go func() {
		log.Info("listening", "addr", cfg.Listen, "role", cfg.Jobs.Role, "ai_service", cfg.AIService.String())
		errc <- srv.ListenAndServe()
	}()
	select {
	case err := <-errc:
		stopJobs()
		waitJobs()
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	case <-ctx.Done():
		// orchestrator.drain_on_stop_signal: no more runs taken from the queue from now on
		jobs.Drain()
		shutdown, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		err := srv.Shutdown(shutdown)
		stopJobs()
		waitJobs()
		return err
	}
	return nil
}
