// The Go gateway (docs/specs/gateway-go.md): answers the routes ported and forwards every
// other request to the Python gateway.
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
	upstream := proxy.New(cfg.Upstream, log)
	routes := append([]server.Route{{Method: http.MethodGet, Pattern: "/health", Handler: server.Health()}},
		api.Routes(&api.Deps{DB: pool, Auth: authn, Panel: panel, Config: cfg, Log: log, Engine: engineClient, Fernet: fernet,
			Upstream: upstream, AIService: proxy.New(cfg.AIService, log), SQL: aisvc.New(cfg.AIService, cfg.FernetKey)})...)
	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           server.New(routes, upstream),
		ReadHeaderTimeout: 30 * time.Second,
		// as uvicorn's --timeout-keep-alive 130 in the production compose
		IdleTimeout: 130 * time.Second,
	}
	errc := make(chan error, 1)
	go func() {
		log.Info("listening", "addr", cfg.Listen, "upstream", cfg.Upstream.String(), "ai_service", cfg.AIService.String())
		errc <- srv.ListenAndServe()
	}()
	select {
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		return srv.Shutdown(shutdown)
	}
	return nil
}
