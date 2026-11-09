// The program of a data API's pods (docs/specs/data-apis.md, «Runtime»): a module of its
// own, built with cgo for DuckDB, so the gateway stays without. Its image is
// tabularia-data-api (dataapi/Dockerfile); the gateway's reconcile loop runs it, one
// Deployment per data API, with the environment internal/pod/config.go reads.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/pod"
)

// At SIGTERM: how long the pod keeps serving while it is out of the readiness, so that the
// Service's endpoints and the ingress controller stop sending it requests first (two seconds
// were not enough: requests still came and waited the shutdown out, measured on kind); then
// how long the requests in flight have, then the last usage. Together within the
// Deployment's terminationGracePeriodSeconds (dataapis.TerminationGraceSeconds).
const (
	drainDelay   = 8 * time.Second
	shutdownWait = 25 * time.Second
	flushWait    = 5 * time.Second
)

// version is the release's, set at the build (-X main.version)
var version = "dev"

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if len(os.Args) > 1 && os.Args[1] == "version" {
		fmt.Println("tabularia-data-api " + version)
		return
	}
	cfg, err := pod.LoadConfig(os.Getenv)
	if err != nil {
		log.Error("configuration", "err", err.Error())
		os.Exit(2)
	}
	if err := os.MkdirAll(cfg.DataDir, 0o700); err != nil {
		log.Error("data directory", "dir", cfg.DataDir, "err", err.Error())
		os.Exit(2)
	}
	// the files a previous run of this container left (its own names only)
	pod.RemoveLeftovers(cfg.DataDir)
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyFromEnvironment, ResponseHeaderTimeout: 30 * time.Second,
		DialContext: (&net.Dialer{Timeout: 10 * time.Second}).DialContext, MaxIdleConnsPerHost: 4}}
	gateway := &pod.Gateway{URL: cfg.GatewayURL, ID: cfg.APIID, Token: cfg.PodToken, HTTP: client}
	prep := &pod.Preparer{Dir: cfg.DataDir, HTTP: client}
	s := pod.NewServer(cfg, log, gateway, prep)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	srv := &http.Server{Addr: ":" + strconv.Itoa(cfg.Port), Handler: s.Handler(), ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout: 30 * time.Second, WriteTimeout: cfg.QueryTimeout + 30*time.Second, IdleTimeout: 120 * time.Second,
		MaxHeaderBytes: 64 << 10}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("listen", "err", err.Error())
			os.Exit(1)
		}
	}()
	log.Info("started", "data_api", cfg.APIID, "port", cfg.Port, "memory_mb", cfg.MemoryMB, "threads", cfg.Threads)
	polled := make(chan struct{})
	go func() {
		s.Run(ctx)
		close(polled)
	}()
	<-ctx.Done()
	// SIGTERM: out of the Service first, then the requests in flight finish, then the last
	// usage goes to the gateway
	log.Info("stopping")
	s.Stop()
	time.Sleep(drainDelay) // the endpoints follow the readiness, then the server stops taking requests
	shutdown, cancel := context.WithTimeout(context.Background(), shutdownWait)
	defer cancel()
	_ = srv.Shutdown(shutdown)
	<-polled
	flush, cancelFlush := context.WithTimeout(context.Background(), flushWait)
	defer cancelFlush()
	s.FlushUsage(flush)
	s.Close()
	log.Info("stopped")
}
