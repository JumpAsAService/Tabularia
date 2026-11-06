package api

import (
	"context"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
)

// validateShortcuts is the PUT's validation of a raw JSON body: the 422's body, or "".
func validateShortcuts(t *testing.T, raw string) (string, []string) {
	t.Helper()
	b, resp := pyd.ReadBody([]byte(raw), "application/json")
	if resp != nil {
		return string(resp.Body), nil
	}
	var ps pyd.Params
	mv := ps.Body(b, flowShortcutsChange)
	if resp := ps.Response(); resp != nil {
		if resp.Status != 422 {
			t.Fatalf("%s: status %d", raw, resp.Status)
		}
		return string(resp.Body), nil
	}
	return "", shortcutList(mv.Get("shortcuts"))
}

func TestFlowShortcutsValidation(t *testing.T) {
	good := []struct {
		body string
		want []string
	}{
		{`{"shortcuts": ["filter", "select", "join"]}`, []string{"filter", "select", "join"}},
		{`{"shortcuts": []}`, []string{}},
		{`{"shortcuts": null}`, nil},
		{`{"shortcuts": ["filter","select","join","group_by","sort","compute","rename","union"]}`,
			[]string{"filter", "select", "join", "group_by", "sort", "compute", "rename", "union"}},
	}
	for _, c := range good {
		bad, got := validateShortcuts(t, c.body)
		if bad != "" || !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s: %q %v", c.body, bad, got)
		}
	}
	bad := []struct{ body, says string }{
		{`{}`, `"type":"missing"`},
		{`{"shortcuts": "filter"}`, `"type":"list_type"`},
		{`{"shortcuts": ["filter", "foreach"]}`, `"type":"literal_error","loc":["body","shortcuts",1]`},
		{`{"shortcuts": ["filter", 3]}`, `"type":"literal_error"`},
		{`{"shortcuts": ["filter", "filter"]}`, `Value error, an operation appears more than once`},
		{`{"shortcuts": ["filter","select","join","group_by","sort","compute","rename","union","limit"]}`, `"type":"too_long"`},
		{`{"shortcuts": [`, `"type":"json_invalid"`},
	}
	for _, c := range bad {
		got, _ := validateShortcuts(t, c.body)
		if !strings.Contains(got, c.says) {
			t.Errorf("%s: %s", c.body, got)
		}
	}
}

func TestFlowShortcutsStoredPerPerson(t *testing.T) {
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
	// in a transaction rolled back at the end: the other packages' tests share the database
	tx, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	var ann, bob int64
	for _, who := range []struct {
		email string
		id    *int64
	}{{"fs-ann@x.test", &ann}, {"fs-bob@x.test", &bob}} {
		if err := tx.QueryRow(ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, '', 'x', true, false, false, false, now()) RETURNING id`, who.email).Scan(who.id); err != nil {
			t.Fatal(err)
		}
	}
	read := func(id int64) []string {
		t.Helper()
		list, err := readFlowShortcuts(ctx, tx, id)
		if err != nil {
			t.Fatal(err)
		}
		return list
	}
	if read(ann) != nil {
		t.Fatal("a new account has chosen nothing")
	}
	if err := writeFlowShortcuts(ctx, tx, ann, []string{"join", "filter"}); err != nil {
		t.Fatal(err)
	}
	if got := read(ann); !reflect.DeepEqual(got, []string{"join", "filter"}) {
		t.Errorf("ann: %v", got)
	}
	if read(bob) != nil {
		t.Error("bob's menu changed with ann's")
	}
	if err := writeFlowShortcuts(ctx, tx, ann, []string{}); err != nil {
		t.Fatal(err)
	}
	if got := read(ann); got == nil || len(got) != 0 {
		t.Errorf("an empty menu stays empty, not the defaults: %v", got)
	}
	if err := writeFlowShortcuts(ctx, tx, ann, nil); err != nil {
		t.Fatal(err)
	}
	if read(ann) != nil {
		t.Error("null goes back to the defaults")
	}
	if _, err := tx.Exec(ctx, `UPDATE users SET flow_shortcuts = '{"not": "a list"}' WHERE id = $1`, ann); err != nil {
		t.Fatal(err)
	}
	if read(ann) != nil {
		t.Error("an unreadable column reads as never chosen")
	}
}
