package modelgroups

import (
	"encoding/json"
	"testing"
)

// The system libraries of a group's image as its Administration card shows them: the customer
// lifetime group's is GSL; the other groups have none, and say so with an empty list.
func TestTheSystemLibrariesAGroupsCardShows(t *testing.T) {
	g, ok := Get("customer_lifetime")
	if !ok {
		t.Fatal("no customer_lifetime group")
	}
	got, _ := json.Marshal(g.SystemLibraries())
	if string(got) != `[{"package":"libgsl-dev","name":"GSL","version":"2.7.1"}]` {
		t.Errorf("customer_lifetime: %s", got)
	}
	for _, other := range []string{"regression", "survival", "timeseries", "boosting"} {
		g, ok := Get(other)
		if !ok {
			continue
		}
		if libs := g.SystemLibraries(); libs == nil || len(libs) != 0 {
			t.Errorf("%s: %v", other, libs)
		}
	}
	// a package this table does not know is shown by its Debian name
	odd := Group{Recipe: Recipe{SystemLibraries: []string{"libfoo-dev"}}}
	got, _ = json.Marshal(odd.SystemLibraries())
	if string(got) != `[{"package":"libfoo-dev","name":"libfoo-dev"}]` {
		t.Errorf("unknown: %s", got)
	}
}
