package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
)

// A provider added from the panel or the API starts with every chat model of its catalog on
// (the AI service reads the catalog); one whose catalog does not answer is saved all the same,
// with no model on and words saying so. On a real database (gateway-go/scripts/test.sh).

// a test key, not a secret of anything
const providersTestFernetKey = "Vd3m7oIM2w3o2r6Y-ZbM6O1t3Qk3c7HnS0WBu6Mxx2E="

func TestANewProviderStartsWithItsChatModelsOn(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	// the auth package's tests TRUNCATE the users: one at a time
	lock, err := db.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lock.Exec(ctx, `SELECT pg_advisory_lock(7101)`); err != nil {
		t.Fatal(err)
	}
	stamp := time.Now().UnixNano()
	var admin auth.User
	admin.Email, admin.FullName, admin.IsActive, admin.IsSuperuser = fmt.Sprintf("aip-%d@x.test", stamp), "admin", true, true
	if err := db.QueryRow(ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
		VALUES ($1, 'admin', 'x', true, true, false, false, now()) RETURNING id`, admin.Email).Scan(&admin.ID); err != nil {
		t.Fatal(err)
	}
	var made []int64
	t.Cleanup(func() {
		for _, id := range made {
			_, _ = db.Exec(ctx, `DELETE FROM ai_models WHERE model_id LIKE $1 || '%'`, fmt.Sprintf("provider:%d/", id))
		}
		_, _ = db.Exec(ctx, `DELETE FROM ai_providers WHERE id = ANY($1)`, made)
		_, _ = db.Exec(ctx, `DELETE FROM audit_logs WHERE actor_id = $1`, admin.ID)
		_, _ = db.Exec(ctx, `DELETE FROM users WHERE id = $1`, admin.ID)
		_, _ = lock.Exec(ctx, `SELECT pg_advisory_unlock(7101)`)
		lock.Release()
		db.Close()
	})

	// the AI service's /internal/ai/catalog, as the case at hand wants it
	var answer func(providerID int64) (int, any)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/ai/catalog" || r.Header.Get(secret.InternalHeader) != secret.InternalToken(providersTestFernetKey) {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		var body struct {
			ProviderID int64 `json:"provider_id"`
		}
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		status, out := answer(body.ProviderID)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(out)
	}))
	t.Cleanup(srv.Close)
	base, _ := url.Parse(srv.URL)
	fernet, err := secret.NewFernet(providersTestFernetKey)
	if err != nil {
		t.Fatal(err)
	}
	d := &Deps{DB: db, Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Config: &config.Config{}, Fernet: fernet,
		SQL: aisvc.New(base, providersTestFernetKey)}

	create := func(name string) (AiProviderOut, map[string]any) {
		t.Helper()
		raw, _ := json.Marshal(map[string]any{"name": fmt.Sprintf("%s %d", name, stamp), "kind": "openai_compatible",
			"base_url": "http://llm.example/v1", "api_key": "not a key"})
		body, resp := pyd.ReadBody(raw, "application/json")
		if resp != nil {
			t.Fatalf("body: %s", resp.Body)
		}
		w := httptest.NewRecorder()
		createAIProvider(d)(w, httptest.NewRequest(http.MethodPost, "/ai/providers", nil), &auth.Principal{User: admin}, body, raw)
		if w.Code != http.StatusCreated {
			t.Fatalf("%s: %d %s", name, w.Code, w.Body.String())
		}
		var out AiProviderOut
		var loose map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &out)
		_ = json.Unmarshal(w.Body.Bytes(), &loose)
		made = append(made, *out.ID)
		return out, loose
	}
	enabled := func(id int64) []string {
		t.Helper()
		rows, err := db.Query(ctx, `SELECT model_id FROM ai_models WHERE enabled AND model_id LIKE $1 || '%' ORDER BY model_id`,
			fmt.Sprintf("provider:%d/", id))
		if err != nil {
			t.Fatal(err)
		}
		defer rows.Close()
		var out []string
		for rows.Next() {
			var m string
			_ = rows.Scan(&m)
			out = append(out, m)
		}
		return out
	}

	t.Run("its chat models are on, the others not", func(t *testing.T) {
		var asked int64
		answer = func(id int64) (int, any) {
			asked = id
			return 200, map[string]any{"models": []string{"text-embedding-3-small", "gpt-x", "whisper-1", "bge-m3", "llama-4", "rerank-2"}, "problem": nil}
		}
		out, loose := create("Fake LLM")
		id := *out.ID
		if asked != id {
			t.Fatalf("the catalog asked for %d, not %d", asked, id)
		}
		want := []string{fmt.Sprintf("provider:%d/gpt-x", id), fmt.Sprintf("provider:%d/llama-4", id)}
		got := enabled(id)
		sort.Strings(got)
		if strings.Join(got, ",") != strings.Join(want, ",") {
			t.Fatalf("enabled %v, want %v", got, want)
		}
		if out.EnabledModels != 2 || out.ModelsProblem != nil {
			t.Fatalf("answer %+v", out)
		}
		if _, has := loose["models_problem"]; has {
			t.Fatalf("models_problem in a good answer: %v", loose)
		}
		var detail string
		if err := db.QueryRow(ctx, `SELECT detail::text FROM audit_logs WHERE actor_id = $1 AND action = 'ai.model_enable'
			AND target_type = 'ai_provider' AND target_id = $2`, admin.ID, id).Scan(&detail); err != nil {
			t.Fatalf("the audit entry: %v", err)
		}
		if !strings.Contains(detail, `"gpt-x"`) || !strings.Contains(detail, `"llama-4"`) || strings.Contains(detail, "embedding") {
			t.Fatalf("audit detail %s", detail)
		}
	})

	t.Run("a catalog that does not answer leaves it saved, with words", func(t *testing.T) {
		answer = func(int64) (int, any) {
			return 200, map[string]any{"models": []string{}, "problem": "«Fake» refused the request (401)."}
		}
		out, _ := create("Refusing LLM")
		if out.EnabledModels != 0 || out.ModelsProblem == nil || !strings.Contains(*out.ModelsProblem, "refused the request (401)") ||
			!strings.Contains(*out.ModelsProblem, "by hand") {
			t.Fatalf("answer %+v", out)
		}
		if got := enabled(*out.ID); len(got) != 0 {
			t.Fatalf("enabled %v", got)
		}
	})

	t.Run("nor does an AI service that does not answer", func(t *testing.T) {
		answer = func(int64) (int, any) { return 500, map[string]any{"detail": "boom"} }
		out, _ := create("Lonely LLM")
		if out.ModelsProblem == nil || !strings.Contains(*out.ModelsProblem, "the AI service did not answer") {
			t.Fatalf("answer %+v", out)
		}
		var n int
		_ = db.QueryRow(ctx, `SELECT count(*) FROM ai_providers WHERE id = $1`, *out.ID).Scan(&n)
		if n != 1 {
			t.Fatal("the provider was not saved")
		}
	})
}
