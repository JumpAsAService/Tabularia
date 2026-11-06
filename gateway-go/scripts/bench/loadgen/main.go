// loadgen: closed-loop HTTP load. N workers, each sends the next request as soon as the
// previous one answers, for a fixed time, cycling over the given paths.
// Prints one JSON line: requests/s, latency percentiles, errors.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

func main() {
	base := flag.String("base", "", "gateway address")
	token := flag.String("token", "", "bearer token")
	paths := flag.String("paths", "/auth/me", "comma-separated paths")
	conc := flag.Int("c", 16, "concurrent workers")
	dur := flag.Duration("d", 30*time.Second, "duration")
	warm := flag.Duration("w", 5*time.Second, "warm-up, not measured")
	label := flag.String("label", "", "label")
	flag.Parse()
	list := strings.Split(*paths, ",")
	tr := &http.Transport{MaxIdleConnsPerHost: *conc * 2, MaxConnsPerHost: *conc * 2}
	client := &http.Client{Transport: tr, Timeout: 60 * time.Second}

	var mu sync.Mutex
	var lat []time.Duration
	errs, codes := 0, map[int]int{}
	start := time.Now()
	measureFrom := start.Add(*warm)
	end := measureFrom.Add(*dur)
	var wg sync.WaitGroup
	for w := 0; w < *conc; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := w; ; i++ {
				t0 := time.Now()
				if t0.After(end) {
					return
				}
				req, _ := http.NewRequest("GET", *base+list[i%len(list)], nil)
				req.Header.Set("Authorization", "Bearer "+*token)
				resp, err := client.Do(req)
				code := 0
				if err == nil {
					io.Copy(io.Discard, resp.Body)
					resp.Body.Close()
					code = resp.StatusCode
				}
				d := time.Since(t0)
				if t0.Before(measureFrom) {
					continue
				}
				mu.Lock()
				if err != nil || code >= 400 {
					errs++
				}
				codes[code]++
				lat = append(lat, d)
				mu.Unlock()
			}
		}(w)
	}
	wg.Wait()
	sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
	pct := func(p float64) float64 {
		if len(lat) == 0 {
			return 0
		}
		return float64(lat[int(p*float64(len(lat)-1))].Microseconds()) / 1000
	}
	out := map[string]any{
		"label": *label, "concurrency": *conc, "requests": len(lat),
		"rps": float64(len(lat)) / dur.Seconds(), "errors": errs, "codes": codes,
		"p50_ms": pct(0.50), "p95_ms": pct(0.95), "p99_ms": pct(0.99), "max_ms": pct(1),
	}
	b, _ := json.Marshal(out)
	fmt.Fprintln(os.Stdout, string(b))
}
