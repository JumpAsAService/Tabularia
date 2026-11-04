package api

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// AppInfoOut is routes/system.py AppInfoOut.
type AppInfoOut struct {
	Name                     string `json:"name"`
	Version                  string `json:"version"`
	Codename                 string `json:"codename"`
	Timezone                 string `json:"timezone"`
	PreviewDefaultSampleRows int    `json:"preview_default_sample_rows"`
}

// pyFloat is a float written as Python's json writes it (1.0, not 1).
type pyFloat float64

func (f pyFloat) MarshalJSON() ([]byte, error) { return []byte(pyjson.FloatRepr(float64(f))), nil }

// MemoryOut is routes/system.py MemoryOut: the host's RAM.
type MemoryOut struct {
	TotalBytes     pyFloat `json:"total_bytes"`
	AvailableBytes pyFloat `json:"available_bytes"`
	UsedBytes      pyFloat `json:"used_bytes"`
	UsedPercent    pyFloat `json:"used_percent"`
}

func init() {
	register("system", func(d *Deps) []server.Route {
		m := &memoryReader{}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/system/info", Handler: server.Authed(d.Auth, "/system/info", func(w http.ResponseWriter, _ *http.Request, _ *auth.Principal) {
				c := d.Config
				server.JSON(w, http.StatusOK, AppInfoOut{Name: c.AppName, Version: c.AppVersion, Codename: c.AppCodename,
					Timezone: d.Panel.AppTimezone(), PreviewDefaultSampleRows: d.Panel.PreviewSampleRows()})
			})},
			{Method: http.MethodGet, Pattern: "/system/memory", Handler: server.Authed(d.Auth, "/system/memory", systemMemory(d, m))},
		}
	})
}

const (
	memTotal     = "node_memory_MemTotal_bytes"
	memAvailable = "node_memory_MemAvailable_bytes"
)

// parseMemory is system._parse: the two metrics from node-exporter's text.
func parseMemory(body string) (*MemoryOut, error) {
	values := map[string]float64{}
	sc := bufio.NewScanner(strings.NewReader(body))
	sc.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for sc.Scan() {
		line := sc.Text()
		for _, name := range []string{memTotal, memAvailable} {
			if strings.HasPrefix(line, name+" ") {
				fields := strings.Fields(line)
				if len(fields) < 2 {
					continue
				}
				// float(): the rest of the line after the first blank run
				_, rest, _ := strings.Cut(strings.TrimLeft(line, " \t"), " ")
				if v, err := pyFloatOf(strings.TrimSpace(rest)); err == nil {
					values[name] = v
				}
			}
		}
	}
	total, okT := values[memTotal]
	available, okA := values[memAvailable]
	if !okT || total == 0 || !okA {
		return nil, errors.New("memory metrics missing from node-exporter's answer")
	}
	used := total - available
	if used < 0 {
		used = 0
	}
	pct, _ := strconv.ParseFloat(strconv.FormatFloat(used/total*100, 'f', 1, 64), 64)
	return &MemoryOut{TotalBytes: pyFloat(total), AvailableBytes: pyFloat(available), UsedBytes: pyFloat(used), UsedPercent: pyFloat(pct)}, nil
}

// pyFloatOf is float() on node-exporter's numbers (1.6e+10, NaN, +Inf).
func pyFloatOf(s string) (float64, error) {
	switch strings.ToLower(s) {
	case "inf", "+inf", "infinity", "+infinity", "-inf", "-infinity", "nan", "+nan", "-nan":
		return strconv.ParseFloat(strings.ToLower(s), 64)
	}
	if strings.ContainsAny(s, "xXpP") {
		return 0, errors.New("not a float")
	}
	return strconv.ParseFloat(strings.ReplaceAll(s, "_", ""), 64)
}

// noRedirects is httpx's client: it does not follow a redirect.
var noRedirects = &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}

// memoryReader is system._read_memory's cache: one read every monitoring.cache_seconds, shared
// by every poller, one at a time.
type memoryReader struct {
	mu    sync.Mutex
	at    time.Time
	value *MemoryOut
}

func (m *memoryReader) read(ctx context.Context, d *Deps) (*MemoryOut, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.value != nil && time.Since(m.at).Seconds() < d.Config.MonitoringCache {
		return m.value, nil
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(d.Config.MonitoringTimeout*float64(time.Second)))
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, d.Config.NodeExporterURL, nil)
	if err != nil {
		return nil, err
	}
	res, err := noRedirects.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, err
	}
	if res.StatusCode < 200 || res.StatusCode > 299 { // httpx's raise_for_status
		return nil, fmt.Errorf("node-exporter answered %d", res.StatusCode)
	}
	v, err := parseMemory(string(raw))
	if err != nil {
		return nil, err
	}
	m.at, m.value = time.Now(), v
	return v, nil
}

// systemMemory is routes/system.py system_memory: 404 without a node-exporter, 503 when it
// does not answer.
func systemMemory(d *Deps, m *memoryReader) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		if strings.TrimSpace(d.Config.NodeExporterURL) == "" {
			detail(w, http.StatusNotFound, "memory badge not configured")
			return
		}
		v, err := m.read(r.Context(), d)
		if err != nil {
			d.Log.Warn("system/memory not available", "err", err)
			detail(w, http.StatusServiceUnavailable, "Memory metrics are not available")
			return
		}
		server.JSON(w, http.StatusOK, v)
	}
}
