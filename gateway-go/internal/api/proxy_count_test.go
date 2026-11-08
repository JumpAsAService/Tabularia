package api

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
)

// «Cancel» on a slow preview stops only the person's own slot: the slot reaches the engine
// prefixed with the user, and a body without a usable slot never gets there.
func TestCancelSendsTheUsersOwnSlotOnly(t *testing.T) {
	var got []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got = append(got, r.URL.Path+" "+string(b))
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"cancelled": true}`))
	}))
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, "a test fernet key, not of anything")
	if err != nil {
		t.Fatal(err)
	}
	d := &Deps{Config: &config.Config{}, Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Engine: e}
	p := &auth.Principal{User: auth.User{ID: 7}}
	call := func(body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		d.proxyCancelSlot(w, httptest.NewRequest(http.MethodPost, "/tasks/preview/cancel", strings.NewReader(body)), p)
		return w
	}
	if w := call(`{"slot": "ed-abc-preview"}`); w.Code != http.StatusOK {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if len(got) != 1 || got[0] != `/tasks/preview/cancel {"slot": "u7:ed-abc-preview"}` {
		t.Fatalf("the engine was asked %q", got)
	}
	for _, bad := range []string{`{}`, `{"slot": "u1:ed-abc-preview"}`, `{"slot": "a b"}`, `{"slot": 3}`, `[]`} {
		if w := call(bad); w.Code != http.StatusUnprocessableEntity {
			t.Errorf("%s: status %d, want 422", bad, w.Code)
		}
	}
	if len(got) != 1 {
		t.Fatalf("a refused body reached the engine: %q", got)
	}
}
