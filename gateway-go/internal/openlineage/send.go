package openlineage

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// ErrNotSent is an event the collector did not take, worded as the client's requests/urllib3
// stack words it where the words are known: a 4xx or 5xx answer («400 Client Error: Bad
// Request for url: …»), the retries spent on 500/502/503/504. A refused connection is
// worded by urllib3 with an object's memory address: here without it.
type ErrNotSent struct{ Message string }

func (e *ErrNotSent) Error() string { return e.Message }

// ErrNoTransport: neither OPENLINEAGE__URL nor OPENLINEAGE__FILE, only the library's own
// configuration (OPENLINEAGE_URL / OPENLINEAGE_CONFIG), which this port does not read.
var ErrNoTransport = errors.New("openlineage: only the library's own configuration is set")

// retry is the client's urllib3 Retry: total 2, connect 2, read 2, backoff 0.3, the statuses
// 500, 502, 503, 504 retried for POST.
var retryStatus = map[int]bool{500: true, 502: true, 503: true, 504: true}

// Emit is _emit: each event sent to the collector, in order (HTTP, and the file when set);
// how many.
func (s Settings) Emit(ctx context.Context, events []pyjson.Object) (int, error) {
	if s.URL == "" && s.File == "" {
		return 0, ErrNoTransport
	}
	n := 0
	for _, e := range events {
		body, err := pyjson.Dumps(e, pyjson.Options{SortKeys: true})
		if err != nil {
			return n, err
		}
		if s.URL != "" {
			if err := s.post(ctx, body); err != nil {
				if s.File == "" {
					return n, err
				}
			}
		}
		if s.File != "" {
			if err := s.write(body); err != nil && s.URL == "" {
				return n, err
			}
		}
		n++
	}
	return n, nil
}

func (s Settings) target() (string, error) {
	base, err := url.Parse(strings.TrimSpace(s.URL))
	if err != nil || base.Scheme == "" || base.Host == "" {
		return "", fmt.Errorf("Need valid url for OpenLineageClient, passed %s", strings.TrimSpace(s.URL))
	}
	ref, err := url.Parse(s.Endpoint)
	if err != nil {
		return "", err
	}
	return base.ResolveReference(ref).String(), nil
}

// post is HttpTransport.emit with its retries.
func (s Settings) post(ctx context.Context, body string) error {
	target, err := s.target()
	if err != nil {
		return &ErrNotSent{err.Error()}
	}
	timeout := time.Duration(s.Timeout * float64(time.Second))
	client := &http.Client{Timeout: timeout}
	u, _ := url.Parse(target)
	port := u.Port()
	if port == "" {
		port = map[string]string{"https": "443"}[u.Scheme]
		if port == "" {
			port = "80"
		}
	}
	pool := "HTTPConnectionPool"
	if u.Scheme == "https" {
		pool = "HTTPSConnectionPool"
	}
	where := fmt.Sprintf("%s(host='%s', port=%s): Max retries exceeded with url: %s", pool, u.Hostname(), port, u.RequestURI())
	for attempt := 0; ; attempt++ {
		if attempt >= 2 { // urllib3's backoff: 0 before the first retry, then 0.3 × 2^(n-1)
			time.Sleep(time.Duration(0.3*float64(int(1)<<(attempt-1))) * time.Second)
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, strings.NewReader(body))
		if err != nil {
			return &ErrNotSent{err.Error()}
		}
		req.Header.Set("Content-Type", "application/json")
		if s.APIKey != "" {
			req.Header.Set("Authorization", "Bearer "+s.APIKey)
		}
		res, err := client.Do(req)
		if err != nil {
			if attempt < 2 {
				continue
			}
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() {
				return &ErrNotSent{fmt.Sprintf("%s (Caused by ReadTimeoutError(\"%s\"))", where, "Read timed out.")}
			}
			return &ErrNotSent{fmt.Sprintf("%s (Caused by NewConnectionError('Failed to establish a new connection: %s'))", where, err)}
		}
		_, _ = io.Copy(io.Discard, res.Body)
		res.Body.Close()
		if retryStatus[res.StatusCode] {
			if attempt < 2 {
				if res.StatusCode == 503 {
					if after, err := strconv.Atoi(strings.TrimSpace(res.Header.Get("Retry-After"))); err == nil && after > 0 {
						time.Sleep(time.Duration(after) * time.Second)
					}
				}
				continue
			}
			return &ErrNotSent{fmt.Sprintf("%s (Caused by ResponseError('too many %d error responses'))", where, res.StatusCode)}
		}
		if res.StatusCode >= 400 {
			kind := "Client"
			if res.StatusCode >= 500 {
				kind = "Server"
			}
			reason := strings.TrimSpace(strings.TrimPrefix(res.Status, strconv.Itoa(res.StatusCode)))
			return &ErrNotSent{fmt.Sprintf("%d %s Error: %s for url: %s", res.StatusCode, kind, reason, target)}
		}
		return nil
	}
}

// write is FileTransport (append): one JSON line per event.
func (s Settings) write(body string) error {
	f, err := os.OpenFile(s.File, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.WriteString(body + "\n")
	return err
}
