package pyd_test

import (
	"bytes"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
)

// A subclass declared by hand with Extend answers as the generated, flattened
// declaration of the same Python class (synthetic.py Child).
func TestExtendMatchesFlattened(t *testing.T) {
	parent := pyd.NewModel("Validated",
		pyd.F("pw", pyd.Str().After(synCheck)).Default(""),
		pyd.F("opt_pw", pyd.Optional(pyd.Str().After(synCheck))).Default(nil),
		pyd.F("folder", pyd.Str()).Default(""),
		pyd.F("pattern", pyd.Str().MinLen(1).MaxLen(5)).Default("x"),
		pyd.F("templates", pyd.Dict(pyd.Str()).MaxLen(2)).DefaultJSON("{}"),
	).FieldValidator(synNoDotDot, "folder", "pattern").FieldValidator(synShort, "templates").After(synWhole)
	child := parent.Extend("Child",
		pyd.F("extra_field", pyd.Int()).Default(0),
		pyd.F("pw", pyd.Str()).Default("over"),
	)
	for _, body := range []string{
		`{}`, `{"pw":"bad","extra_field":"x"}`, `{"folder":"boom"}`, `{"folder":"..","pattern":"","extra_field":1.5}`,
		`{"templates":{"a":"12345"},"opt_pw":"bad"}`,
	} {
		got, want := answer422(t, child, body), answer422(t, synChild, body)
		if !bytes.Equal(got, want) {
			t.Errorf("%s:\n  Extend:    %s\n  generated: %s", body, got, want)
		}
	}
	// the parent keeps its own field and none of the child's
	if got := answer422(t, parent, `{"pw":"bad","extra_field":"x"}`); !bytes.Contains(got, []byte(`Value error, bad value`)) {
		t.Errorf("the parent lost its validator: %s", got)
	}
}

func answer422(t *testing.T, m *pyd.Model, body string) []byte {
	t.Helper()
	b, early := pyd.ReadBody([]byte(body), "application/json")
	if early != nil {
		return early.Body
	}
	var p pyd.Params
	mv := p.Body(b, m)
	if r := p.Response(); r != nil {
		return r.Body
	}
	out, err := pyd.DumpsLenient(mv.Dump())
	if err != nil {
		t.Fatal(err)
	}
	return out
}
