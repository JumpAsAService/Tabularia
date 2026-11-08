package api

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// A column profile reads the step's data as a preview does: someone who may not read the
// source, or a source outside the engine's bucket, never gets to the engine; whoever may
// read it reaches the engine with the slot made their own, so a click elsewhere or
// «Cancel» stops only their profile.
func TestProfileIsCheckedAsAPreviewAndScopesItsSlot(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var got []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got = append(got, r.URL.Path+" "+string(b))
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows": 3, "columns": {}, "how": "computed"}`))
	}))
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, "a test fernet key, not of anything")
	if err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{EngineBucket: "data-prep"}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	d := &Deps{DB: db, Config: cfg, Log: log, Engine: e, Panel: settings.New(cfg, false, log)}
	user := func(email string, admin bool) auth.User {
		var id int64
		if err := db.QueryRow(ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, '', 'x', true, $2, false, false, now()) RETURNING id`, email, admin).Scan(&id); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _, _ = db.Exec(ctx, `DELETE FROM users WHERE id = $1`, id) })
		return auth.User{ID: id, Email: email, IsActive: true, IsSuperuser: admin}
	}
	reader := &auth.Principal{User: user("pf-reader@x.test", false)}
	admin := &auth.Principal{User: user("pf-admin@x.test", true)}
	call := func(p *auth.Principal, body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		d.proxyProfile(w, httptest.NewRequest(http.MethodPost, "/tasks/profile", strings.NewReader(body)), p)
		return w
	}
	body := `{"bucket": "data-prep", "input_key": "datasets/pf-nobody.parquet", "operations": [], "columns": [{"name": "a", "dtype": "Int64"}], "slot": "ed-abc-profile"}`
	if w := call(reader, body); w.Code != http.StatusForbidden {
		t.Fatalf("a source the person may not read: status %d (%s)", w.Code, w.Body.String())
	}
	other := strings.Replace(body, `"bucket": "data-prep"`, `"bucket": "elsewhere"`, 1)
	if w := call(reader, other); w.Code != http.StatusForbidden {
		t.Fatalf("another bucket: status %d (%s)", w.Code, w.Body.String())
	}
	if len(got) != 0 {
		t.Fatalf("a refused profile reached the engine: %q", got)
	}
	w := call(admin, body)
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"rows"`) {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	want := fmt.Sprintf(`"slot": "u%d:ed-abc-profile"`, admin.User.ID)
	if len(got) != 1 || !strings.HasPrefix(got[0], "/tasks/profile ") || !strings.Contains(got[0], want) {
		t.Fatalf("the engine was asked %q, want the path and %s", got, want)
	}
}
