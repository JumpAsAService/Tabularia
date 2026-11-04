package api

import "testing"

func TestRegistryHosts(t *testing.T) {
	for in, ok := range map[string]bool{" HTTPS://Registry.io:5000/ ": true, "a b": false, "x.io:123456": false, "-x.io": false} {
		if _, r := regHostOf(in); (r == nil) != ok {
			t.Errorf("%q: %v", in, r)
		}
	}
	if regIntersect("team/*", "team/x") != "team/x" || regIntersect("team/*", "team") != "" || regIntersect("*", "a") != "a" {
		t.Error("intersections")
	}
}
