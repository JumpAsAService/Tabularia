package modelgroups

import (
	"encoding/json"
	"testing"
)

// The system libraries of a group's image as its Administration card shows them: the label is a
// name; the version comes from the lock of the version built, never from this package.
func TestTheSystemLibrariesAGroupsCardShows(t *testing.T) {
	g, ok := Get("customer_lifetime")
	if !ok {
		t.Fatal("no customer_lifetime group")
	}
	say := func(libs []SystemLibrary) string {
		b, _ := json.Marshal(libs)
		return string(b)
	}
	// before a build: the name only, no version
	if got := say(g.SystemLibraries(nil)); got != `[{"package":"libgsl-dev","name":"GSL"}]` {
		t.Errorf("before a build: %s", got)
	}
	lock := "R6@2.6.1\nlibgsl-dev@2.9.9+test-1\nCLVTools@0.12.1"
	if got := say(g.SystemLibraries(&lock)); got != `[{"package":"libgsl-dev","name":"GSL","version":"2.9.9","package_version":"2.9.9+test-1"}]` {
		t.Errorf("after a build: %s", got)
	}
	// a lock of a build that recorded none (an older version): no version
	old := "R6@2.6.1"
	if got := say(g.SystemLibraries(&old)); got != `[{"package":"libgsl-dev","name":"GSL"}]` {
		t.Errorf("an old lock: %s", got)
	}
	// the time series group's Rlibeemd links GSL too: the card shows the library there
	ts, _ := Get("timeseries")
	if got := say(ts.SystemLibraries(&lock)); got != `[{"package":"libgsl-dev","name":"GSL","version":"2.9.9","package_version":"2.9.9+test-1"}]` {
		t.Errorf("timeseries: %s", got)
	}
	for _, other := range []string{"regression", "survival", "boosting", "experiments"} {
		g, ok := Get(other)
		if !ok {
			continue
		}
		if libs := g.SystemLibraries(&lock); libs == nil || len(libs) != 0 {
			t.Errorf("%s: %v", other, libs)
		}
	}
	// a package without a label is shown by its Debian name
	odd := Group{Recipe: Recipe{SystemLibraries: []string{"libfoo-dev"}}}
	if got := say(odd.SystemLibraries(nil)); got != `[{"package":"libfoo-dev","name":"libfoo-dev"}]` {
		t.Errorf("unknown: %s", got)
	}
}

// The library's own version out of a Debian package version: no epoch, no packaging revision, no
// repack suffix; the distribution is never named by hand.
func TestTheUpstreamVersionOfAPackage(t *testing.T) {
	for in, want := range map[string]string{
		"2.7.1+dfsg-6ubuntu2": "2.7.1", "1:2.7.1+dfsg-6": "2.7.1", "2.7.1-3": "2.7.1", "2.7.1": "2.7.1",
		"2.8~rc1-1": "2.8", "1:3.0-2build1": "3.0", "": "", "2.7.1+dfsg": "2.7.1",
	} {
		if got := UpstreamVersion(in); got != want {
			t.Errorf("%q: %q, want %q", in, got, want)
		}
	}
}
