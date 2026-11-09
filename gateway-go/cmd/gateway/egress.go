package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/db"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/egress"
)

// runEgress is APP__ROLE=egress-proxy (internal/egress): the way out of the scripts and
// notebooks without internet access, towards the storage only. It reads the database for
// the scripts bucket (Administration → Scripting environments → the bucket) and nothing
// else of the configuration:
//
//	EGRESS__LISTEN           the forward proxy (":3128")
//	EGRESS__TRANSPARENT      the transparent ports, listen=storage port each ("20000=443,20001=80")
//	EGRESS__MAX_CONNECTIONS  at most at once (1024), EGRESS__MAX_PER_CLIENT from one pod (256)
//	EGRESS__IDLE_SECONDS     a tunnel where nothing passes is closed after (300)
//	STORAGE__ENDPOINT, STORAGE__BUCKET, STORAGE__REGION   the storage of the chart's values
func runEgress(log *slog.Logger) error {
	env := config.FromOS()
	get := func(name, def string) string {
		if v, ok := env(name); ok && strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
		return def
	}
	number := func(name string, def int) (int, error) {
		v := get(name, "")
		if v == "" {
			return def, nil
		}
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			return 0, fmt.Errorf("configuration: %s is not a positive whole number: %q", name, v)
		}
		return n, nil
	}
	maxConns, err := number("EGRESS__MAX_CONNECTIONS", 1024)
	if err != nil {
		return err
	}
	perClient, err := number("EGRESS__MAX_PER_CLIENT", 256)
	if err != nil {
		return err
	}
	idle, err := number("EGRESS__IDLE_SECONDS", 300)
	if err != nil {
		return err
	}
	transparent, err := transparentPorts(get("EGRESS__TRANSPARENT", ""))
	if err != nil {
		return err
	}
	static := egress.HostsOf(get("STORAGE__ENDPOINT", ""), get("STORAGE__REGION", ""), get("STORAGE__BUCKET", ""))

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	pool, err := db.Open(ctx, config.DSN(env), 2)
	if err != nil {
		return err
	}
	defer pool.Close()

	var allowed atomic.Pointer[map[string]bool]
	refresh := allowlist(pool, static, log)
	refresh(ctx, &allowed)
	go func() {
		t := time.NewTicker(15 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				refresh(ctx, &allowed)
			}
		}
	}()
	proxy := &egress.Proxy{
		Allowed:  func() map[string]bool { return *allowed.Load() },
		Log:      log,
		MaxConns: maxConns, MaxPerClient: perClient, IdleTimeout: time.Duration(idle) * time.Second,
	}
	addr := get("EGRESS__LISTEN", ":3128")
	l, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	errc := make(chan error, 1+len(transparent))
	go func() { errc <- proxy.ServeForward(ctx, l) }()
	var ports []string
	for listen, port := range transparent {
		tl, err := net.Listen("tcp", fmt.Sprintf(":%d", listen))
		if err != nil {
			return err
		}
		ports = append(ports, fmt.Sprintf("%d→%d", listen, port))
		go func(port int) { errc <- proxy.ServeTransparent(ctx, tl, port) }(port)
	}
	sort.Strings(ports)
	log.Info("egress proxy listening", "forward", addr, "transparent", strings.Join(ports, ","), "max_connections", maxConns)
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
		// no new connection from now on (the listeners close with ctx); the tunnels open
		// finish, up to the pod's grace period
		proxy.Wait(context.Background())
		return nil
	}
}

// transparentPorts reads "20000=443,20001=80": the port listened on → the storage's port.
func transparentPorts(s string) (map[int]int, error) {
	out := map[int]int{}
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		l, p, ok := strings.Cut(part, "=")
		listen, err1 := strconv.Atoi(strings.TrimSpace(l))
		port, err2 := strconv.Atoi(strings.TrimSpace(p))
		if !ok || err1 != nil || err2 != nil || listen < 1 || listen > 65535 || port < 1 || port > 65535 {
			return nil, fmt.Errorf("configuration: EGRESS__TRANSPARENT: %q is not listen=port", part)
		}
		out[listen] = port
	}
	return out, nil
}

// allowlist reads the scripts bucket and makes the list: the chart's storage and the bucket's
// endpoint, with their virtual-hosted names. The database unreachable, the last list stays.
func allowlist(pool *pgxpool.Pool, static []string, log *slog.Logger) func(context.Context, *atomic.Pointer[map[string]bool]) {
	var last string
	failing := false
	return func(ctx context.Context, into *atomic.Pointer[map[string]bool]) {
		hosts := append([]string(nil), static...)
		var endpoint, region, bucket string
		qctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		err := pool.QueryRow(qctx, `SELECT endpoint, region, bucket FROM script_storage WHERE id = 1`).Scan(&endpoint, &region, &bucket)
		cancel()
		switch {
		case err == nil:
			failing = false
			if strings.TrimSpace(bucket) != "" {
				hosts = append(hosts, egress.HostsOf(endpoint, region, bucket)...)
			}
		case errors.Is(err, pgx.ErrNoRows):
			failing = false
		default:
			if !failing {
				log.Warn("egress proxy: the scripts bucket could not be read, the last list stays", "err", err.Error())
			}
			failing = true
			if into.Load() != nil {
				return
			}
		}
		set := map[string]bool{}
		for _, h := range hosts {
			set[h] = true
		}
		keys := make([]string, 0, len(set))
		for h := range set {
			keys = append(keys, h)
		}
		sort.Strings(keys)
		if joined := strings.Join(keys, ","); joined != last {
			last = joined
			log.Info("egress proxy: the hosts it reaches", "hosts", joined)
		}
		into.Store(&set)
	}
}
