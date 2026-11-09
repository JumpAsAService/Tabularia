package pod

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// Gateway asks the gateway for the manifest and sends it the usage, with the pod token.
type Gateway struct {
	URL   string
	ID    int64
	Token string
	HTTP  *http.Client
}

// the most a manifest may weigh: a few thousand keys
const maxManifestBytes = 16 << 20

// ErrTokenRefused: the gateway does not know the pod token (401): the data API was deleted,
// or the pods are being replaced with a new one. Not a passing failure.
var ErrTokenRefused = errors.New("the gateway refused the pod token")

// Manifest is the data API's manifest now.
func (g *Gateway) Manifest(ctx context.Context) (*dataapiwire.Manifest, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf("%s/internal/data-apis/%d/manifest", g.URL, g.ID), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+g.Token)
	res, err := g.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, maxManifestBytes))
	if err != nil {
		return nil, err
	}
	if res.StatusCode == http.StatusUnauthorized {
		return nil, ErrTokenRefused
	}
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("the gateway answered %d to the manifest", res.StatusCode)
	}
	var m dataapiwire.Manifest
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("the manifest: %w", err)
	}
	if m.ID != g.ID {
		return nil, fmt.Errorf("the manifest is of data API %d, not %d", m.ID, g.ID)
	}
	return &m, nil
}

// SendUsage sends the counts of the last minutes.
func (g *Gateway) SendUsage(ctx context.Context, u dataapiwire.Usage) error {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	body, err := json.Marshal(u)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, fmt.Sprintf("%s/internal/data-apis/%d/usage", g.URL, g.ID),
		bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+g.Token)
	req.Header.Set("Content-Type", "application/json")
	res, err := g.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 1<<16))
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("the gateway answered %d to the usage", res.StatusCode)
	}
	return nil
}
