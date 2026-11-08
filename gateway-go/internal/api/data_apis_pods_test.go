package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
)

// The pods' manifest and usage on a real database: only the pod token of that very data API
// opens them; the manifest follows the creator's authority now, the keys and the data files.

// a fake engine that signs links for the datasets/ keys (each a file of 250 MB) and has no
// `datasets/missing.parquet`
func dapiFakeEngine(t *testing.T, f *dapiFixture) *[]string {
	asked := &[]string{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/files/presigned" || r.Header.Get(secret.InternalHeader) == "" {
			http.NotFound(w, r)
			return
		}
		key := r.URL.Query().Get("key")
		*asked = append(*asked, key+" "+r.URL.Query().Get("expires"))
		if key == "datasets/missing.parquet" {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail": "it has no data yet: refresh it first"}`))
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"url": "http://storage.test/" + r.URL.Query().Get("bucket") + "/" + key + "?sig=1",
			"size": 250 << 20})
	}))
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, "a test fernet key, not of anything")
	if err != nil {
		t.Fatal(err)
	}
	f.d.Engine = e
	return asked
}

func (f *dapiFixture) pod(token string, h podHandler, method string, id int64, body []byte) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, "/internal/data-apis/x", bytes.NewReader(body))
	r.SetPathValue("api_id", dapiID(id))
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	dataAPIPod(f.d, h).ServeHTTP(w, r)
	return w
}

func (f *dapiFixture) manifest(token string, id int64) dataapiwire.Manifest {
	f.t.Helper()
	w := f.pod(token, podManifest, http.MethodGet, id, nil)
	if w.Code != http.StatusOK {
		f.t.Fatalf("manifest: %d %s", w.Code, w.Body.String())
	}
	return decode[dataapiwire.Manifest](f.t, w)
}

func TestDataAPIPodToken(t *testing.T) {
	f := newDapiFixture(t)
	dapiFakeEngine(t, f)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	b, _ := f.create(f.creator, f.folder, map[string]any{"name": "ERP", "slug": f.slug("erp"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	// no token yet: nothing opens it
	never, _ := dataapiwire.Random(dataapiwire.PodTokenPrefix)
	if w := f.pod(never, podManifest, http.MethodGet, a.ID, nil); w.Code != http.StatusUnauthorized || w.Header().Get("WWW-Authenticate") != "Bearer" {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	tokenA, err := dataapis.RotatePodToken(f.ctx, f.db, a.ID)
	if err != nil {
		t.Fatal(err)
	}
	tokenB, err := dataapis.RotatePodToken(f.ctx, f.db, b.ID)
	if err != nil {
		t.Fatal(err)
	}
	if !dataapiwire.WellFormed(tokenA, "tdp_") || tokenA == tokenB {
		t.Fatal(tokenA, tokenB)
	}
	var kept string
	if err := f.db.QueryRow(f.ctx, `SELECT pod_token_hash FROM data_apis WHERE id = $1`, a.ID).Scan(&kept); err != nil ||
		kept != dataapiwire.Hash(tokenA) || strings.Contains(kept, tokenA[4:]) {
		t.Fatalf("%q %v", kept, err)
	}
	if m := f.manifest(tokenA, a.ID); m.ID != a.ID || m.Slug != a.Slug {
		t.Fatalf("%+v", m)
	}
	// another API's token, a wrong one, none, a session-like text, a key of the API: refused
	_, _ = dataapis.RotatePodToken(f.ctx, f.db, 0) // no such data API: an error, nothing changed
	for _, bad := range []string{tokenB, tokenA[:len(tokenA)-1] + "x", "", "tdk_" + tokenA[4:], "Bearer"} {
		for _, h := range []podHandler{podManifest, podUsage} {
			if w := f.pod(bad, h, http.MethodGet, a.ID, []byte(`{"minutes": []}`)); w.Code != http.StatusUnauthorized {
				t.Errorf("%q: %d %s", bad, w.Code, w.Body.String())
			}
		}
	}
	// the right token on the other API's path
	if w := f.pod(tokenA, podManifest, http.MethodGet, b.ID, nil); w.Code != http.StatusUnauthorized {
		t.Errorf("%d", w.Code)
	}
	// a person's way in is not a pod's: the token in another scheme
	r := httptest.NewRequest(http.MethodGet, "/x", nil)
	r.SetPathValue("api_id", dapiID(a.ID))
	r.Header.Set("Authorization", "Basic "+tokenA)
	w := httptest.NewRecorder()
	dataAPIPod(f.d, podManifest).ServeHTTP(w, r)
	if w.Code != http.StatusUnauthorized {
		t.Errorf("basic: %d", w.Code)
	}
	// rotated: the old one stops at once
	again, err := dataapis.RotatePodToken(f.ctx, f.db, a.ID)
	if err != nil || again == tokenA {
		t.Fatal(err)
	}
	if w := f.pod(tokenA, podManifest, http.MethodGet, a.ID, nil); w.Code != http.StatusUnauthorized {
		t.Errorf("old token: %d", w.Code)
	}
	f.manifest(again, a.ID)
	// the token is not a session of the app: the routes of a person refuse it
	asPerson := httptest.NewRequest(http.MethodGet, "/data-apis", nil)
	asPerson.Header.Set("Authorization", "Bearer "+again)
	if p, refusal := f.d.Auth.Authenticate(f.ctx, asPerson, "/data-apis"); p != nil || refusal == nil {
		t.Error("the pod token signs a person in")
	}
}

func TestDataAPIManifest(t *testing.T) {
	f := newDapiFixture(t)
	asked := dapiFakeEngine(t, f)
	_, _ = f.db.Exec(f.ctx, `UPDATE datasources SET bucket = 'tabularia', key = 'datasets/customers.parquet' WHERE id = $1`, f.customers)
	_, _ = f.db.Exec(f.ctx, `UPDATE datasources SET bucket = 'tabularia', key = 'datasets/missing.parquet' WHERE id = $1`, f.orders)
	secretRes := f.resource("salaries", f.secret)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm"),
		"resources": []any{f.resource("customers", f.customers), f.resource("orders", f.orders), secretRes}}, http.StatusCreated)
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"replicas": 2, "cpu": "500m", "memory": "512Mi"},
		map[string]string{"api_id": dapiID(a.ID)}); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	token, err := dataapis.RotatePodToken(f.ctx, f.db, a.ID)
	if err != nil {
		t.Fatal(err)
	}
	// a draft: its state only
	m := f.manifest(token, a.ID)
	if m.State != "draft" || len(m.Resources) != 0 || len(m.Keys) != 0 || m.PollSeconds != 20 {
		t.Fatalf("%+v", m)
	}
	path := map[string]string{"api_id": dapiID(a.ID)}
	expires := time.Now().Add(30 * 24 * time.Hour).UTC().Format(time.RFC3339)
	w := f.call(f.creator, createDataAPIKey, http.MethodPost, map[string]any{"name": "Salesforce", "expires_at": expires,
		"rate_per_minute": 120, "resources": []string{"customers"}, "cidrs": []string{"203.0.113.0/24"}}, path)
	if w.Code != http.StatusCreated {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	made := decode[dapiKeyAnswer](t, w)
	w = f.call(f.creator, createDataAPIKey, http.MethodPost, map[string]any{"name": "Old", "expires_at": expires}, path)
	old := decode[dapiKeyAnswer](t, w)
	f.call(f.creator, revokeDataAPIKey, http.MethodPost, nil, map[string]string{"api_id": dapiID(a.ID), "key_id": dapiID(old.Key.ID)})
	if w := f.call(f.creator, activateDataAPI, http.MethodPost, nil, path); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	// an activation drops the pod token; the reconcile loop makes the next one
	if w := f.pod(token, podManifest, http.MethodGet, a.ID, nil); w.Code != http.StatusUnauthorized {
		t.Fatalf("the token of before the activation: %d", w.Code)
	}
	token, _ = dataapis.RotatePodToken(f.ctx, f.db, a.ID)
	m = f.manifest(token, a.ID)
	if m.State != "active" || m.Replicas != 2 || len(m.Resources) != 3 || len(m.Keys) != 2 {
		t.Fatalf("%+v", m)
	}
	cust, orders, salaries := m.Resources[0], m.Resources[1], m.Resources[2]
	if cust.Name != "customers" || !cust.Available || cust.URL != "http://storage.test/tabularia/datasets/customers.parquet?sig=1" ||
		cust.Version == "" || len(cust.Fingerprint) != 24 || strings.Join(cust.Columns, ",") != "id,name,region,signup" ||
		len(cust.Filters) != 2 || cust.Filters[0].Column != "id" || strings.Join(cust.Filters[0].Operators, ",") != "eq,in" ||
		strings.Join(cust.DefaultSort, ",") != "id" || cust.MaxPage != 500 {
		t.Errorf("%+v", cust)
	}
	if orders.Available || orders.Unavailable != "no_data" || orders.URL != "" {
		t.Errorf("%+v", orders)
	}
	// the creator reads the secret folder: available, but its datasource has no data file
	if salaries.Available || salaries.Unavailable != "no_data" {
		t.Errorf("%+v", salaries)
	}
	if strings.Join(*asked, ";") != "datasets/customers.parquet 300;datasets/missing.parquet 300" {
		t.Errorf("%v", *asked)
	}
	var key, revoked dataapiwire.Key
	for _, k := range m.Keys {
		if k.ID == made.Key.ID {
			key = k
		} else {
			revoked = k
		}
	}
	if key.Hash != dataapiwire.Hash(made.Secret) || key.Hint != made.Secret[4:12] || key.RatePerMinute != 120 ||
		strings.Join(key.Resources, ",") != "customers" || strings.Join(key.CIDRs, ",") != "203.0.113.0/24" || key.State(time.Now()) != "active" {
		t.Errorf("%+v", key)
	}
	if revoked.RevokedAt == nil || revoked.State(time.Now()) != "revoked" || len(revoked.Resources) != 0 {
		t.Errorf("%+v", revoked)
	}
	raw, _ := json.Marshal(m)
	if strings.Contains(string(raw), made.Secret) || strings.Contains(string(raw), token) {
		t.Fatal("a secret in the manifest")
	}
	// a refresh: another file, another fingerprint
	before := cust
	_, _ = f.db.Exec(f.ctx, `UPDATE datasources SET key = 'datasets/customers-2.parquet', updated_at = now() WHERE id = $1`, f.customers)
	if c := f.manifest(token, a.ID).Resources[0]; c.Fingerprint == before.Fingerprint || !strings.Contains(c.URL, "customers-2") {
		t.Errorf("%+v", c)
	}
	// the creator no longer reads the secret folder: that resource only
	_, _ = f.db.Exec(f.ctx, `UPDATE datasources SET bucket = 'tabularia', key = 'datasets/salaries.parquet' WHERE id = $1`, f.secret)
	if s := f.manifest(token, a.ID).Resources[2]; !s.Available {
		t.Fatalf("%+v", s)
	}
	_, _ = f.db.Exec(f.ctx, `DELETE FROM permissions WHERE project_id = $1 AND user_id = $2`, f.secretFolder, f.creator.ID)
	m = f.manifest(token, a.ID)
	if s := m.Resources[2]; m.State != "active" || s.Available || s.Unavailable != "datasource_rights" || s.URL != "" || !m.Resources[0].Available {
		t.Errorf("%+v", m)
	}
	// MANAGE taken away: the whole API is suspended, nothing of it given
	_, _ = f.db.Exec(f.ctx, `UPDATE permissions SET capability = 'edit' WHERE project_id = $1 AND user_id = $2`, f.folder, f.creator.ID)
	m = f.manifest(token, a.ID)
	if m.State != "suspended" || m.Suspended != "creator_rights" || len(m.Resources) != 0 || len(m.Keys) != 0 {
		t.Errorf("%+v", m)
	}
	_, _ = f.db.Exec(f.ctx, `UPDATE permissions SET capability = 'manage' WHERE project_id = $1 AND user_id = $2`, f.folder, f.creator.ID)
	if m = f.manifest(token, a.ID); m.State != "active" {
		t.Errorf("%+v", m)
	}
	// deactivated
	_, _ = f.db.Exec(f.ctx, `UPDATE users SET is_active = false WHERE id = $1`, f.creator.ID)
	if m = f.manifest(token, a.ID); m.State != "suspended" || m.Suspended != "creator_inactive" || len(m.Keys) != 0 {
		t.Errorf("%+v", m)
	}
	_, _ = f.db.Exec(f.ctx, `UPDATE users SET is_active = true WHERE id = $1`, f.creator.ID)
	// paused, and the feature switched off
	if w := f.call(f.creator, pauseDataAPI, http.MethodPost, nil, path); w.Code != http.StatusOK {
		t.Fatalf("%d", w.Code)
	}
	if m = f.manifest(token, a.ID); m.State != "paused" || len(m.Keys) != 0 {
		t.Errorf("%+v", m)
	}
	f.call(f.creator, activateDataAPI, http.MethodPost, nil, path)
	token, _ = dataapis.RotatePodToken(f.ctx, f.db, a.ID)
	f.d.Config.DataAPI.Enabled = false
	if m = f.manifest(token, a.ID); m.State != "disabled" || len(m.Resources) != 0 {
		t.Errorf("%+v", m)
	}
	f.d.Config.DataAPI.Enabled = true
	f.d.Config.DataAPI.PollSeconds = 7
	if m = f.manifest(token, a.ID); m.PollSeconds != 7 {
		t.Errorf("%+v", m)
	}
}

func TestDataAPIUsage(t *testing.T) {
	f := newDapiFixture(t)
	dapiFakeEngine(t, f)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	b, _ := f.create(f.creator, f.folder, map[string]any{"name": "ERP", "slug": f.slug("erp"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	token, _ := dataapis.RotatePodToken(f.ctx, f.db, a.ID)
	expires := time.Now().Add(30 * 24 * time.Hour).UTC().Format(time.RFC3339)
	key := decode[dapiKeyAnswer](t, f.call(f.creator, createDataAPIKey, http.MethodPost, map[string]any{"name": "CRM", "expires_at": expires},
		map[string]string{"api_id": dapiID(a.ID)})).Key
	other := decode[dapiKeyAnswer](t, f.call(f.creator, createDataAPIKey, http.MethodPost, map[string]any{"name": "ERP", "expires_at": expires},
		map[string]string{"api_id": dapiID(b.ID)})).Key
	minute := time.Now().UTC().Truncate(time.Minute)
	send := func(u dataapiwire.Usage) *httptest.ResponseRecorder {
		raw, _ := json.Marshal(u)
		return f.pod(token, podUsage, http.MethodPost, a.ID, raw)
	}
	w := send(dataapiwire.Usage{Pod: "crm-1", Minutes: []dataapiwire.UsageMinute{
		{KeyID: key.ID, Minute: minute.Add(17 * time.Second), Requests: 10, Rows: 100, Errors: 1, Throttled: 2},
		{KeyID: key.ID, Minute: minute.Add(-time.Minute), Requests: 3, Rows: 3},
		{KeyID: other.ID, Minute: minute, Requests: 5},                      // another API's key
		{KeyID: key.ID, Minute: minute.Add(-48 * time.Hour), Requests: 5},   // too old
		{KeyID: key.ID, Minute: minute.Add(time.Hour), Requests: 5},         // ahead
		{KeyID: key.ID, Minute: minute.Add(-2 * time.Minute), Requests: -4}, // negative
	}})
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"kept":2`) || !strings.Contains(w.Body.String(), `"left_out":4`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	// another pod, the same minute: added
	send(dataapiwire.Usage{Pod: "crm-2", Minutes: []dataapiwire.UsageMinute{{KeyID: key.ID, Minute: minute, Requests: 1, Rows: 7}}})
	var req, rows, errs, throttled int64
	if err := f.db.QueryRow(f.ctx, `SELECT requests, rows, errors, throttled FROM data_api_usage WHERE key_id = $1 AND minute = $2`,
		key.ID, minute).Scan(&req, &rows, &errs, &throttled); err != nil || req != 11 || rows != 107 || errs != 1 || throttled != 2 {
		t.Fatalf("%d %d %d %d %v", req, rows, errs, throttled, err)
	}
	var n int
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM data_api_usage WHERE key_id = $1`, other.ID).Scan(&n)
	if n != 0 {
		t.Error("another API's key counted")
	}
	var last *time.Time
	_ = f.db.QueryRow(f.ctx, `SELECT last_used_at FROM data_api_keys WHERE id = $1`, key.ID).Scan(&last)
	if last == nil || !last.Equal(minute) {
		t.Errorf("%v", last)
	}
	counts, _ := dataapis.Requests24h(f.ctx, f.db, a.ID)
	if counts[key.ID] != 14 {
		t.Errorf("%v", counts)
	}
	// what is not a report
	if w := f.pod(token, podUsage, http.MethodPost, a.ID, []byte(`{"minutes": "x"}`)); w.Code != http.StatusUnprocessableEntity {
		t.Errorf("%d", w.Code)
	}
	big := dataapiwire.Usage{Minutes: make([]dataapiwire.UsageMinute, dataapiwire.MaxUsageMinutes+1)}
	if w := send(big); w.Code != http.StatusUnprocessableEntity {
		t.Errorf("%d", w.Code)
	}
	if w := f.pod(token, podUsage, http.MethodPost, a.ID, bytes.Repeat([]byte(" "), dataAPIUsageMaxBytes+1)); w.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("%d", w.Code)
	}
}

// While autoscaling, a key's rate is divided by the pods running now (the autoscaler's count),
// not by the fewest: scaled out, the pods together would let through many times the rate.
func TestDataAPIManifestAutoscaleReplicas(t *testing.T) {
	f := newDapiFixture(t)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "Scaled", "slug": f.slug("scaled"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	path := map[string]string{"api_id": dapiID(a.ID)}
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"autoscale": true, "min_replicas": 2,
		"max_replicas": 8, "cpu": "500m", "memory": "512Mi"}, path); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w := f.call(f.creator, activateDataAPI, http.MethodPost, nil, path); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	token, _ := dataapis.RotatePodToken(f.ctx, f.db, a.ID)
	if m := f.manifest(token, a.ID); m.Replicas != 2 {
		t.Errorf("before the loop saw the cluster: %d replicas, want the fewest (2)", m.Replicas)
	}
	for _, c := range []struct{ wanted, want int }{{6, 6}, {1, 2}, {20, 8}} {
		if err := dataapis.SetClusterStatus(f.ctx, f.db, a.ID, dataapis.ClusterStatus{Wanted: c.wanted}); err != nil {
			t.Fatal(err)
		}
		if m := f.manifest(token, a.ID); m.Replicas != c.want {
			t.Errorf("the autoscaler at %d: %d replicas, want %d", c.wanted, m.Replicas, c.want)
		}
	}
}
