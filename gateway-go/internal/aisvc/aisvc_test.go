package aisvc

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
)

// a test key, not a secret of anything
const key = "Vd3m7oIM2w3o2r6Y-ZbM6O1t3Qk3c7HnS0WBu6Mxx2E="

func service(t *testing.T, handler func(path string, body map[string]any) (int, any)) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get(secret.InternalHeader) != secret.InternalToken(key) || r.Method != http.MethodPost {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		status, answer := handler(r.URL.Path, body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(answer)
	}))
	t.Cleanup(srv.Close)
	u, _ := url.Parse(srv.URL + "/")
	return New(u, key)
}

func TestTheCallsCarryTheTokenAndReadTheAnswers(t *testing.T) {
	c := service(t, func(path string, body map[string]any) (int, any) {
		switch path {
		case "/internal/sql/tables":
			if body["dialect"] != "duckdb" || body["sql"] != "SELECT * FROM t" {
				return 400, nil
			}
			return 200, map[string]any{"error": nil, "statements": 1, "query": true, "ctes": []string{},
				"tables": []any{map[string]any{"name": "t", "folder": "", "catalog": false, "written": "", "aliased": false, "start": 14, "end": 14}}}
		case "/internal/sql/without-params":
			if body["dialect"] != "sqlserver" || len(body["names"].([]any)) != 1 {
				return 400, nil
			}
			return 200, map[string]any{"sql": "SELECT 1 WHERE 1 = 1"}
		case "/internal/sql/qualified-tables":
			return 200, map[string]any{"tables": []string{"public.orders"}}
		}
		return 404, nil
	})
	ctx := context.Background()
	p, err := c.Tables(ctx, "SELECT * FROM t", "duckdb")
	if err != nil || p.Statements != 1 || !p.Query || len(p.Tables) != 1 || *p.Tables[0].Start != 14 || p.Error != nil {
		t.Fatalf("%+v %v", p, err)
	}
	if sql, err := c.WithoutParams(ctx, "x", "sqlserver", []string{"a"}); err != nil || sql != "SELECT 1 WHERE 1 = 1" {
		t.Errorf("%q %v", sql, err)
	}
	if tables, err := c.QualifiedTables(ctx, "x", "postgresql"); err != nil || len(tables) != 1 {
		t.Errorf("%v %v", tables, err)
	}
}

func TestARefusalIsSaidAndAFailureIsAnError(t *testing.T) {
	c := service(t, func(path string, body map[string]any) (int, any) {
		if path == "/internal/sql/without-params" {
			return 422, map[string]any{"detail": "write {a} without quotes"}
		}
		return 422, map[string]any{"detail": []any{map[string]any{"loc": []any{"body"}}}}
	})
	_, err := c.WithoutParams(context.Background(), "x", "", nil)
	var r *Refusal
	if !errors.As(err, &r) || r.Message != "write {a} without quotes" {
		t.Errorf("%v", err)
	}
	if _, err := c.Tables(context.Background(), "x", "duckdb"); err == nil || errors.As(err, &r) {
		t.Errorf("a validation error is not a refusal: %v", err)
	}
	var none *Client
	if _, err := none.Tables(context.Background(), "x", "duckdb"); !errors.Is(err, ErrNoClient) {
		t.Error(err)
	}
}
