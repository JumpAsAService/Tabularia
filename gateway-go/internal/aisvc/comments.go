package aisvc

import (
	"context"
	"time"
)

// CommentsTimeout bounds /internal/ai/comments: the numbers the engine reads for them
// (reports._numbers_of_comments, 300 s) and the comments themselves (AI__COMMENT_WAIT_SECONDS,
// 240 s at most), with a margin.
const CommentsTimeout = 600 * time.Second

// Comments is /internal/ai/comments (routes/internal_ai.py): reports.comments_for for one
// document of a Pushmail cell. `body` is the request already encoded (its keys in Python's
// order: the definitions are read as they were written); the answer's bytes are
// {"comments": {…}, "reading": text | null}, for the caller to read in order.
func (c *Client) Comments(ctx context.Context, body []byte) ([]byte, error) {
	if c == nil {
		return nil, ErrNoClient
	}
	return c.postRaw(ctx, "/internal/ai/comments", body, CommentsTimeout)
}

// Catalog is what /internal/ai/catalog answers: a provider's models, or why they could not be read.
type Catalog struct {
	Models  []string `json:"models"`  // the names at the provider
	Problem *string  `json:"problem"` // nil: the catalog was read
}

// Catalog is /internal/ai/catalog (routes/internal_ai.py): the catalog of a saved provider,
// read as the panel and PUT /ai/models read it (Anthropic's through its SDK), fresh. A
// provider that does not answer is a Problem, not an error: the error is the service's.
func (c *Client) Catalog(ctx context.Context, providerID int64) (*Catalog, error) {
	if c == nil {
		return nil, ErrNoClient
	}
	var out Catalog
	if err := c.post(ctx, "/internal/ai/catalog", map[string]any{"provider_id": providerID}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
