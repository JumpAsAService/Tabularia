// Package aisvc is the client of the AI service's internal API (docs/specs/gateway-go.md, «The
// AI service»; gateway/app/routes/internal_sql.py): the SQL parsing the Python gateway does with
// sqlglot, which has no faithful Go equivalent. One call per use of sqlglot; what is done with
// the answer (who may read what, the refusals' words) is the Go code's. The service answers only
// who carries the internal token the engine also asks for (secret.InternalToken).
package aisvc

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
)

// Timeout bounds one call: a parse takes milliseconds, a service that does not answer must not
// hold the request for ever.
const Timeout = 30 * time.Second

// Client calls the AI service at its base URL.
type Client struct {
	base  *url.URL
	token string
	http  *http.Client
}

// New is the client of the service at `base`, with the token derived from the Fernet key.
func New(base *url.URL, fernetKey string) *Client {
	noRedirects := func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return &Client{base: base, token: secret.InternalToken(fernetKey), http: &http.Client{CheckRedirect: noRedirects}}
}

// Refusal is a 422 of the service whose detail is a text: what the Python function raised
// (report_params._refuse), to be said as the Python code says it.
type Refusal struct{ Message string }

func (r *Refusal) Error() string { return r.Message }

func (c *Client) post(ctx context.Context, path string, body, out any) error {
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, Timeout)
	defer cancel()
	u := *c.base
	u.Path = strings.TrimSuffix(u.Path, "/") + path
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u.String(), bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(secret.InternalHeader, c.token)
	res, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("aisvc: %s: %w", path, err)
	}
	defer res.Body.Close()
	answer, err := io.ReadAll(res.Body)
	if err != nil {
		return fmt.Errorf("aisvc: %s: %w", path, err)
	}
	if res.StatusCode == http.StatusUnprocessableEntity {
		var refused struct {
			Detail any `json:"detail"`
		}
		if json.Unmarshal(answer, &refused) == nil {
			if text, ok := refused.Detail.(string); ok {
				return &Refusal{Message: text}
			}
		}
	}
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("aisvc: %s answered %d", path, res.StatusCode)
	}
	return json.Unmarshal(answer, out)
}

// Table is a table a SQL names, as sqlglot read it.
type Table struct {
	Name    string `json:"name"`    // table.name
	Folder  string `json:"folder"`  // table.db
	Catalog bool   `json:"catalog"` // a catalog qualifies it
	Written string `json:"written"` // table.sql(dialect), for one with a catalog
	Aliased bool   `json:"aliased"` // it has an alias
	Start   *int   `json:"start"`   // where its folder (or its name) starts, in characters
	End     *int   `json:"end"`     // where its name ends (its last character)
}

// Parsed is what /internal/sql/tables answers.
type Parsed struct {
	Error      *string  `json:"error"`      // sqlglot's refusal: it could not read the SQL
	Statements int      `json:"statements"` // how many statements
	Query      bool     `json:"query"`      // the only one is a query
	CTEs       []string `json:"ctes"`       // the CTEs' names, lower case (Python's lower)
	Tables     []Table  `json:"tables"`     // of the only statement, in sqlglot's order
}

// ErrNoClient is a call made without the service configured.
var ErrNoClient = errors.New("aisvc: GATEWAY_GO__AI_SERVICE is not configured")

// Tables is what sql_scripts.resolve and _named_tables read of `sql` in a sqlglot dialect.
func (c *Client) Tables(ctx context.Context, sql, dialect string) (*Parsed, error) {
	if c == nil {
		return nil, ErrNoClient
	}
	var out Parsed
	if err := c.post(ctx, "/internal/sql/tables", map[string]any{"sql": sql, "dialect": dialect}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// WithoutParams is report_params.without_params(sql, dialect_of(dialectKey), names); a refusal
// is a *Refusal.
func (c *Client) WithoutParams(ctx context.Context, sql, dialectKey string, names []string) (string, error) {
	if c == nil {
		return "", ErrNoClient
	}
	if names == nil {
		names = []string{}
	}
	var out struct {
		SQL string `json:"sql"`
	}
	if err := c.post(ctx, "/internal/sql/without-params", map[string]any{"sql": sql, "dialect": dialectKey, "names": names}, &out); err != nil {
		return "", err
	}
	return out.SQL, nil
}

// QualifiedTables is openlineage._qualified_tables(db_type, sql).
func (c *Client) QualifiedTables(ctx context.Context, sql, dbType string) ([]string, error) {
	if c == nil {
		return nil, ErrNoClient
	}
	var out struct {
		Tables []string `json:"tables"`
	}
	if err := c.post(ctx, "/internal/sql/qualified-tables", map[string]any{"sql": sql, "db_type": dbType}, &out); err != nil {
		return nil, err
	}
	return out.Tables, nil
}
