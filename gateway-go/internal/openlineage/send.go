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
// how many. A RunEvent gets the client's own `tags` run facet on the way (update_event_tags_facets:
// the client's version). With OPENLINEAGE_DISABLED=true the client's transport is a no-op:
// nothing goes out, every event counts as sent.
func (s Settings) Emit(ctx context.Context, events []pyjson.Object) (int, error) {
	if s.Disabled {
		return len(events), nil
	}
	if s.URL == "" && s.File == "" {
		if s.LibraryURL == "" || s.LibraryConfig != "" || s.libraryTransport {
			return 0, ErrNoTransport
		}
		// the library's own OPENLINEAGE_URL (_http_transport_from_env_variables): its defaults
		legacy := s
		legacy.URL, legacy.Endpoint, legacy.APIKey, legacy.Timeout, legacy.retries = s.LibraryURL, s.LibraryEndpoint, s.LibraryAPIKey, 5, 5
		if legacy.Endpoint == "" {
			legacy.Endpoint = "api/v1/lineage"
		}
		return legacy.Emit(ctx, events)
	}
	n := 0
	for _, e := range events {
		body, err := pyjson.Dumps(withClientTags(e), pyjson.Options{SortKeys: true})
		if err != nil {
			return n, err
		}
		var sent, failed error
		if s.URL != "" {
			if failed = s.post(ctx, body); failed != nil && s.File == "" {
				return n, failed
			}
		}
		if s.File != "" {
			if sent = s.write(body); sent != nil {
				if s.URL == "" {
					return n, sent
				}
				if failed != nil { // CompositeTransport: none of the two took it
					return n, errors.New("None of the transports successfully emitted the event: " +
						"['<HttpTransport(name=None, kind=http, priority=0)>', '<FileTransport(name=None, kind=file, priority=0)>']")
				}
			}
		}
		n++
	}
	return n, nil
}

// withClientTags is the client's update_event_tags_facets on a RunEvent: the run's `tags`
// facet with the client's version (no tags of the user's: the configuration has none).
func withClientTags(e pyjson.Object) pyjson.Object {
	if _, ok := e.Get("eventType"); !ok {
		return e
	}
	run, ok := e.Value("run").(pyjson.Object)
	if !ok {
		return e
	}
	facets, _ := run.Value("facets").(pyjson.Object)
	facets = append(append(pyjson.Object{}, facets...), pyjson.KV{Key: "tags", Value: facet(std("1-0-0", "TagsRunFacet"),
		pyjson.KV{Key: "tags", Value: []any{pyjson.Object{{Key: "key", Value: "openlineage_client_version"},
			{Key: "value", Value: ClientVersion}, {Key: "source", Value: "OPENLINEAGE_CLIENT"}}}})})
	run = append(pyjson.Object{}, run...).Set("facets", facets)
	return append(pyjson.Object{}, e...).Set("run", run)
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
	retries := s.retries
	if retries == 0 {
		retries = 2 // openlineage._get_client's: total 2, connect 2, read 2
	}
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
			if attempt < retries {
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
			if attempt < retries {
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
