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
	answer := func(id string) map[string]any {
		g, ok := modelgroups.Get(id)
		if !ok {
			t.Fatalf("no group %s", id)
		}
		out, err := mgOut(context.Background(), nil, &mgState{Group: g})
		if err != nil {
			t.Fatal(err)
		}
		raw, _ := json.Marshal(out)
		var m map[string]any
		_ = json.Unmarshal(raw, &m)
		return m
	}
	libs, _ := answer("customer_lifetime")["system_libraries"].([]any)
	if len(libs) != 1 {
		t.Fatalf("customer_lifetime: %v", libs)
	}
	if l, _ := libs[0].(map[string]any); l["package"] != "libgsl-dev" || l["name"] != "GSL" || l["version"] != "2.7.1" {
		t.Errorf("the library: %v", libs[0])
	}
	if none, ok := answer("regression")["system_libraries"].([]any); !ok || len(none) != 0 {
		t.Errorf("regression: %v", answer("regression")["system_libraries"])
	}
}
