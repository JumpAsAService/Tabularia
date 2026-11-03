// Package engine is core/engine_client.py: the client towards the engine (the data plane),
// which answers only who carries the internal token.
package engine

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
)

// Check is the CHECK timeout of the background work's control calls: 30 s, 5 to connect.
const Check = 30 * time.Second

// Client calls the engine at its base URL. No timeout of its own, as the Python client
// (`timeout=None`): big uploads and exports must not expire; callers that need one pass a
// context with a deadline.
type Client struct {
	base  *url.URL
	token string
	http  *http.Client
}

// New is get_engine_client.
func New(baseURL, fernetKey string) (*Client, error) {
	u, err := url.Parse(baseURL)
	if err != nil {
		return nil, err
	}
	return &Client{base: u, token: secret.InternalToken(fernetKey), http: &http.Client{}}, nil
}

// Request builds a request to the engine's `path` (with its query), with the internal token.
func (c *Client) Request(ctx context.Context, method, path string, body io.Reader) (*http.Request, error) {
	ref, err := url.Parse(path)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base.ResolveReference(ref).String(), body)
	if err != nil {
		return nil, err
	}
	req.Header.Set(secret.InternalHeader, c.token)
	return req, nil
}

// Do sends a request built with Request.
func (c *Client) Do(req *http.Request) (*http.Response, error) { return c.http.Do(req) }

// JSON sends `body` as JSON (nil: no body) and returns the answer, its body read.
func (c *Client) JSON(ctx context.Context, method, path string, body any) (*http.Response, []byte, error) {
	var r io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, nil, err
		}
		r = bytes.NewReader(raw)
	}
	req, err := c.Request(ctx, method, path, r)
	if err != nil {
		return nil, nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	res, err := c.http.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(res.Body)
	return res, raw, err
}
