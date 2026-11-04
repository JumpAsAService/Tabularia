package api

import "testing"

func TestClaimAt(t *testing.T) {
	claims := map[string]any{"groups": []any{"/a", " b "}, "resource_access": map[string]any{"tabularia.gateway": map[string]any{"roles": []any{"r"}}},
		"realm_access.roles": "dotted"}
	if v := claimAt(claims, "resource_access.tabularia.gateway.roles"); v == nil || v.([]any)[0] != "r" {
		t.Fatal(v)
	}
	if v := claimAt(claims, "realm_access.roles"); v != "dotted" {
		t.Fatal(v)
	}
	if v := claimAt(claims, "nope.x"); v != nil {
		t.Fatal(v)
	}
	got := normalizeGroups(claims, oidcCfg{GroupsClaim: "groups, realm_access.roles"}, nil)
	if !got["a"] || !got["b"] || !got["dotted"] || len(got) != 3 {
		t.Fatal(got)
	}
}

func TestClaimIsTrue(t *testing.T) {
	for v, want := range map[any]bool{true: true, false: false, "TRUE": true, " yes ": true, "1": true, "false": false, nil: false} {
		if claimIsTrue(v) != want {
			t.Errorf("%v", v)
		}
	}
	if !claimIsTrue(float64(1)) || claimIsTrue(float64(0)) {
		t.Error("numbers")
	}
}
