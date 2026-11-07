package dataapis

import (
	"strings"
	"testing"
	"time"
)

func TestSlugs(t *testing.T) {
	for _, ok := range []string{"crm", "crm-customers", "a1", strings.Repeat("a", 63)} {
		if CheckSlug(ok) != "" {
			t.Errorf("%q refused", ok)
		}
	}
	for _, bad := range []string{"", "-crm", "crm-", "CRM", "crm_customers", "crm/x", "crm.json", strings.Repeat("a", 64), "è"} {
		if CheckSlug(bad) == "" {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestRuntimeWithinTheBounds(t *testing.T) {
	rt, err := CheckRuntime(Runtime{Replicas: 2, CPU: " 500m ", Memory: "512Mi"}, 4)
	if err != nil || rt.CPU != "500m" || rt.MinReplicas != 2 || rt.MaxReplicas != 2 || rt.Pods() != 2 {
		t.Fatalf("%+v %v", rt, err)
	}
	rt, err = CheckRuntime(Runtime{Autoscale: true, MinReplicas: 1, MaxReplicas: 3, Replicas: 9, CPU: "1", Memory: "1Gi"}, 4)
	if err != nil || rt.Replicas != 1 || rt.Pods() != 3 {
		t.Fatalf("%+v %v", rt, err)
	}
	// far past what the chart used to allow one data API: the administrator's to decide
	if rt, err = CheckRuntime(Runtime{Replicas: 12, CPU: "8", Memory: "32Gi"}, 100); err != nil || rt.Pods() != 12 {
		t.Fatalf("%+v %v", rt, err)
	}
	if _, err = CheckRuntime(Runtime{Replicas: 1, CPU: "16", Memory: "64Gi"}, 100); err != nil {
		t.Errorf("the largest pod: %v", err)
	}
	if _, err = CheckRuntime(Runtime{Replicas: 1, CPU: "100m", Memory: "256Mi"}, 100); err != nil {
		t.Errorf("the smallest pod: %v", err)
	}
	for _, bad := range []struct {
		rt    Runtime
		total int
		why   string
	}{
		{Runtime{Replicas: 0, CPU: "500m", Memory: "512Mi"}, 4, "at least 1 replica"},
		{Runtime{Replicas: 5, CPU: "500m", Memory: "512Mi"}, 4, "at most 4 replicas"},
		{Runtime{Replicas: 41, CPU: "500m", Memory: "512Mi"}, 1000, "at most 40 replicas"},
		{Runtime{Autoscale: true, MinReplicas: 2, MaxReplicas: 1, CPU: "500m", Memory: "512Mi"}, 4, "autoscaling"},
		{Runtime{Autoscale: true, MinReplicas: 1, MaxReplicas: 5, CPU: "500m", Memory: "512Mi"}, 4, "at most 4 replicas"},
		{Runtime{Replicas: 1, CPU: "50m", Memory: "512Mi"}, 4, "at least 100m"},
		{Runtime{Replicas: 1, CPU: "17", Memory: "512Mi"}, 4, "at most 16"},
		{Runtime{Replicas: 1, CPU: "a lot", Memory: "512Mi"}, 4, "not a CPU quantity"},
		{Runtime{Replicas: 1, CPU: "500m", Memory: "65Gi"}, 4, "at most 64Gi"},
		{Runtime{Replicas: 1, CPU: "500m", Memory: "128Mi"}, 4, "at least 256Mi"},
		{Runtime{Replicas: 1, CPU: "500m", Memory: "512MB"}, 4, "not a memory quantity"},
	} {
		if _, err := CheckRuntime(bad.rt, bad.total); err == nil || !strings.Contains(err.Error(), bad.why) {
			t.Errorf("%+v: %v", bad.rt, err)
		}
	}
	if d := DefaultRuntime(); d.Pods() != 1 || d.CPU != DefaultCPU || d.Memory != DefaultMemory {
		t.Errorf("the default: %+v", d)
	}
	if _, err := CheckRuntime(DefaultRuntime(), 1); err != nil {
		t.Errorf("the default refused: %v", err)
	}
}

func TestSameRuntime(t *testing.T) {
	base := Runtime{Replicas: 2, MinReplicas: 2, MaxReplicas: 2, CPU: "1", Memory: "1Gi"}
	for _, same := range []Runtime{
		base,
		{Replicas: 2, CPU: "1000m", Memory: "1024Mi"}, // the unused numbers, the spelling
		{Replicas: 2, MinReplicas: 9, CPU: " 1 ", Memory: "1Gi"},
	} {
		if !SameRuntime(base, same) {
			t.Errorf("%+v differs", same)
		}
	}
	for _, other := range []Runtime{
		{Replicas: 3, CPU: "1", Memory: "1Gi"},
		{Replicas: 2, CPU: "500m", Memory: "1Gi"},
		{Replicas: 2, CPU: "1", Memory: "2Gi"},
		{Autoscale: true, MinReplicas: 2, MaxReplicas: 2, CPU: "1", Memory: "1Gi"},
		{Replicas: 2, CPU: "x", Memory: "1Gi"},
	} {
		if SameRuntime(base, other) {
			t.Errorf("%+v the same", other)
		}
	}
	auto := Runtime{Autoscale: true, Replicas: 1, MinReplicas: 1, MaxReplicas: 3, CPU: "250m", Memory: "256Mi"}
	if !SameRuntime(auto, Runtime{Autoscale: true, Replicas: 7, MinReplicas: 1, MaxReplicas: 3, CPU: "250m", Memory: "256Mi"}) ||
		SameRuntime(auto, Runtime{Autoscale: true, MinReplicas: 1, MaxReplicas: 4, CPU: "250m", Memory: "256Mi"}) {
		t.Error("autoscaling compared on its minimum and maximum")
	}
}

func source() map[int64]*Datasource {
	return map[int64]*Datasource{7: {ID: 7, Name: "Customers", Columns: []Column{{"id", "Int64"}, {"name", "String"},
		{"signup", "Date"}, {"tags", "List(String)"}, {"score", "Float64"}}}}
}

func resource() Resource {
	return Resource{Name: "customers", Source: Source{ID: 7}, Columns: []string{"id", "name", "signup", "id"},
		Filters:     []Filter{{Column: "id", Operators: []string{"in", "eq"}}, {Column: "signup", Operators: []string{"gte", "lt"}}},
		DefaultSort: []string{"-signup"}}
}

func TestADefinitionIsCheckedAgainstItsDatasource(t *testing.T) {
	def, err := CheckDefinition(Definition{Resources: []Resource{resource()}}, source())
	if err != nil {
		t.Fatal(err)
	}
	r := def.Resources[0]
	if strings.Join(r.Columns, ",") != "id,name,signup" || r.Source.Kind != SourceDatasource || r.MaxPage != DefaultPage ||
		strings.Join(r.Filters[0].Operators, ",") != "eq,in" {
		t.Fatalf("%+v", r)
	}
	bad := func(why string, change func(*Resource)) {
		t.Helper()
		r := resource()
		change(&r)
		if _, err := CheckDefinition(Definition{Resources: []Resource{r}}, source()); err == nil || !strings.Contains(err.Error(), why) {
			t.Errorf("%s: %v", why, err)
		}
	}
	bad("is not in the datasource", func(r *Resource) { r.Columns = append(r.Columns, "secret") })
	bad("at least one column", func(r *Resource) { r.Columns = nil })
	bad("does not go out", func(r *Resource) { r.Filters = append(r.Filters, Filter{Column: "score", Operators: []string{"eq"}}) })
	bad("only numbers and dates", func(r *Resource) { r.Filters = []Filter{{Column: "name", Operators: []string{"gt"}}} })
	bad("cannot be filtered on", func(r *Resource) {
		r.Columns = append(r.Columns, "tags")
		r.Filters = []Filter{{Column: "tags", Operators: []string{"eq"}}}
	})
	bad("is not an operator", func(r *Resource) { r.Filters = []Filter{{Column: "id", Operators: []string{"like"}}} })
	bad("at least one operator", func(r *Resource) { r.Filters = []Filter{{Column: "id"}} })
	bad("two filters", func(r *Resource) { r.Filters = append(r.Filters, Filter{Column: "id", Operators: []string{"eq"}}) })
	bad("not among the columns one may filter on", func(r *Resource) { r.DefaultSort = []string{"name"} })
	bad("largest page", func(r *Resource) { r.MaxPage = MaxPage + 1 })
	bad("may not read it", func(r *Resource) { r.Source.ID = 8 })
	bad("is not available", func(r *Resource) { r.Source.Kind = "connection" })
	bad("lowercase letters", func(r *Resource) { r.Name = "Customers" })
	bad("kept for the API itself", func(r *Resource) { r.Name = "health" })
	if _, err := CheckDefinition(Definition{Resources: []Resource{resource(), resource()}}, source()); err == nil ||
		!strings.Contains(err.Error(), "two resources") {
		t.Errorf("two resources with one name: %v", err)
	}
}

func TestKeys(t *testing.T) {
	key, hint, hash, err := NewKey()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(key, "tdk_") || len(key) != 44 || hint != key[4:12] || hash != HashKey(key) || len(hash) != 64 ||
		!WellFormedKey(key) {
		t.Fatalf("%q %q %q", key, hint, hash)
	}
	other, _, _, _ := NewKey()
	if other == key {
		t.Fatal("two keys alike")
	}
	for _, bad := range []string{"", "tdk_", key[:43], key + "a", "tab_" + key[4:], strings.Replace(key, key[10:11], "-", 1)} {
		if WellFormedKey(bad) {
			t.Errorf("%q well formed", bad)
		}
	}
	now := time.Date(2026, 11, 7, 12, 0, 0, 0, time.UTC)
	k := &Key{ExpiresAt: now.Add(time.Hour)}
	if KeyState(k, now) != KeyActive {
		t.Error(KeyState(k, now))
	}
	later := now.Add(10 * time.Minute)
	k.GraceUntil = &later
	if KeyState(k, now) != KeyGrace || KeyState(k, later) != KeyReplaced || !Answers(KeyState(k, now)) || Answers(KeyState(k, later)) {
		t.Error("grace")
	}
	k.RevokedAt = &now
	if KeyState(k, now) != KeyRevoked || Answers(KeyRevoked) {
		t.Error("revoked")
	}
	if KeyState(&Key{ExpiresAt: now}, now) != KeyExpired {
		t.Error("expired")
	}
	if _, err := KeyExpiry("", 365, now); err == nil {
		t.Error("an expiry is required")
	}
	if _, err := KeyExpiry(now.Add(-time.Hour).Format(time.RFC3339), 365, now); err == nil {
		t.Error("in the past")
	}
	if _, err := KeyExpiry(now.Add(367*24*time.Hour).Format(time.RFC3339), 365, now); err == nil {
		t.Error("past the longest life")
	}
	if e, err := KeyExpiry(now.Add(30*24*time.Hour).Format(time.RFC3339), 365, now); err != nil || !e.Equal(now.Add(30*24*time.Hour)) {
		t.Error(e, err)
	}
}

func TestCIDRs(t *testing.T) {
	got, err := CIDRs([]string{" 203.0.113.7 ", "198.51.100.12/24", "198.51.100.0/24", "", "2001:db8::1"})
	if err != nil || strings.Join(got, " ") != "203.0.113.7/32 198.51.100.0/24 2001:db8::1/128" {
		t.Fatal(got, err)
	}
	for _, bad := range [][]string{{"example.com"}, {"10.0.0.0/33"}, {"300.1.1.1"}} {
		if _, err := CIDRs(bad); err == nil {
			t.Errorf("%v accepted", bad)
		}
	}
	if _, err := CIDRs(make([]string, MaxCIDRs+1)); err == nil {
		t.Error("too many")
	}
}
