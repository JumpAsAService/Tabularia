package api

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
)

// The groups' answer carries the system libraries of the group's image (the customer lifetime
// group's GSL), and an empty list, not null, for the groups that have none.
func TestTheGroupsAnswerCarriesItsSystemLibraries(t *testing.T) {
	answer := func(id string, lock *string) map[string]any {
		g, ok := modelgroups.Get(id)
		if !ok {
			t.Fatalf("no group %s", id)
		}
		s := &mgState{Group: g}
		if lock != nil {
			id := int64(1)
			s.Versions = []mgVersion{{ID: id, Number: 1, Status: "ready", Lock: lock}}
			s.CurrentID = &id
		}
		out, err := mgOut(context.Background(), nil, s)
		if err != nil {
			t.Fatal(err)
		}
		raw, _ := json.Marshal(out)
		var m map[string]any
		_ = json.Unmarshal(raw, &m)
		return m
	}
	// with no version built: the name, no version
	libs, _ := answer("customer_lifetime", nil)["system_libraries"].([]any)
	if len(libs) != 1 {
		t.Fatalf("customer_lifetime: %v", libs)
	}
	if l, _ := libs[0].(map[string]any); l["package"] != "libgsl-dev" || l["name"] != "GSL" || l["version"] != nil {
		t.Errorf("before a build: %v", libs[0])
	}
	// with a version in use: the version its build recorded in the lock (whatever it is)
	lock := "R6@2.6.1\nlibgsl-dev@9.8.7-test"
	libs, _ = answer("customer_lifetime", &lock)["system_libraries"].([]any)
	if l, _ := libs[0].(map[string]any); l["version"] != "9.8.7-test" {
		t.Errorf("after a build: %v", libs[0])
	}
	if none, ok := answer("regression", nil)["system_libraries"].([]any); !ok || len(none) != 0 {
		t.Errorf("regression: %v", answer("regression", nil)["system_libraries"])
	}
}
