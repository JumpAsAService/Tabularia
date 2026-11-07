package dataapis

import (
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

var caps = config.DataAPIConfig{Enabled: true, MaxAPIs: 5, MaxReplicas: 3, MaxTotalReplicas: 4, MaxCPU: "1", MaxMemory: "1Gi",
	KeyMaxDays: 365, KeyDefaultRate: 600}

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

func TestRuntimeWithinTheCaps(t *testing.T) {
	rt, err := CheckRuntime(Runtime{Replicas: 2, CPU: " 500m ", Memory: "512Mi"}, caps)
	if err != nil || rt.CPU != "500m" || rt.MinReplicas != 2 || rt.MaxReplicas != 2 || rt.Pods() != 2 {
		t.Fatalf("%+v %v", rt, err)
	}
	rt, err = CheckRuntime(Runtime{Autoscale: true, MinReplicas: 1, MaxReplicas: 3, Replicas: 9, CPU: "1", Memory: "1Gi"}, caps)
	if err != nil || rt.Replicas != 1 || rt.Pods() != 3 {
		t.Fatalf("%+v %v", rt, err)
	}
	for _, bad := range []Runtime{
		{Replicas: 0, CPU: "500m", Memory: "512Mi"},
		{Replicas: 4, CPU: "500m", Memory: "512Mi"},
		{Autoscale: true, MinReplicas: 2, MaxReplicas: 1, CPU: "500m", Memory: "512Mi"},
		{Autoscale: true, MinReplicas: 1, MaxReplicas: 4, CPU: "500m", Memory: "512Mi"},
		{Replicas: 1, CPU: "1.5", Memory: "512Mi"},
		{Replicas: 1, CPU: "10m", Memory: "512Mi"},
		{Replicas: 1, CPU: "a lot", Memory: "512Mi"},
		{Replicas: 1, CPU: "500m", Memory: "2Gi"},
		{Replicas: 1, CPU: "500m", Memory: "64Mi"},
		{Replicas: 1, CPU: "500m", Memory: "512MB"},
	} {
		if _, err := CheckRuntime(bad, caps); err == nil {
			t.Errorf("%+v accepted", bad)
		}
	}
	small := caps
	small.MaxCPU, small.MaxMemory = "100m", "200Mi"
	if d := DefaultRuntime(small); d.CPU != "100m" || d.Memory != "200Mi" {
		t.Errorf("the default above the caps: %+v", d)
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
