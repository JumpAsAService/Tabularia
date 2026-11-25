package api

import (
	"net/http"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The XMLA interface's switches (docs/specs/xmla.md, piece 1) on a real database: the
// installation's, the data API's, the 409 of one without the other, and what the pods are told.

type xmlaLimitsOut struct {
	Enabled     bool `json:"enabled"`
	MaxCells    int  `json:"max_cells"`
	MaxTuples   int  `json:"max_tuples"`
	MaxMembers  int  `json:"max_members"`
	KeepRefused bool `json:"keep_refused"`
}

func (f *dapiFixture) xmlaOn() {
	f.d.Config.DataAPI.XMLA = config.XMLAConfig{Enabled: true, MaxCells: 300_000, MaxTuples: 4000, MaxMembers: 900, KeepRefused: true}
}

func TestXMLAOffInTheInstallation(t *testing.T) {
	f := newDapiFixture(t)
	// off by default: the limits say it is not offered, with no caps to show
	limits := decode[struct {
		Enabled bool          `json:"enabled"`
		Xmla    xmlaLimitsOut `json:"xmla"`
	}](t, f.call(f.creator, dataAPILimits, http.MethodGet, nil, nil))
	if !limits.Enabled || limits.Xmla != (xmlaLimitsOut{}) {
		t.Errorf("limits: %+v", limits)
	}
	admin := decode[map[string]any](t, f.call(f.admin, adminListDataAPIsRoute, http.MethodGet, nil, nil))
	if x, _ := admin["xmla"].(map[string]any); x["enabled"] != false || x["max_cells"] != nil {
		t.Errorf("the administration: %v", admin["xmla"])
	}
	// a data API saved with it on is refused, with a code
	for _, body := range []map[string]any{
		{"name": "Pivot", "slug": f.slug("pv"), "xmla": map[string]any{"enabled": true}, "resources": []any{f.resource("customers", f.customers)}},
	} {
		_, w := f.create(f.creator, f.folder, body, http.StatusConflict)
		if got := decode[map[string]string](t, w); got["code"] != "xmla_off" || !strings.Contains(got["detail"], "DATA_API__XMLA_ENABLED") {
			t.Errorf("%v", got)
		}
	}
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "Plain", "slug": f.slug("pl"), "xmla": map[string]any{"enabled": false},
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	path := map[string]string{"api_id": dapiID(a.ID)}
	w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"xmla": map[string]any{"enabled": true}}, path)
	if w.Code != http.StatusConflict || decode[map[string]string](t, w)["code"] != "xmla_off" {
		t.Errorf("turning it on: %d %s", w.Code, w.Body.String())
	}
	// the page that sends it back off, with the rest, is fine; nothing was saved by the refusal
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"name": "Plain 2", "xmla": map[string]any{"enabled": false}}, path); w.Code != http.StatusOK {
		t.Errorf("%d %s", w.Code, w.Body.String())
	}
	if v := decode[dapiDetail](t, f.call(f.creator, getDataAPI, http.MethodGet, nil, path)); v.Xmla.Enabled || v.CurrentVersion != 1 {
		t.Errorf("%+v", v)
	}
	// with the installation's switch on but the data APIs off, it is not offered either
	f.xmlaOn()
	f.d.Config.DataAPI.Enabled = false
	limits2 := decode[map[string]any](t, f.call(f.creator, dataAPILimits, http.MethodGet, nil, nil))
	if limits2["enabled"] != false || limits2["xmla"] != nil {
		t.Errorf("%v", limits2)
	}
	if xmlaOn(f.d) {
		t.Error("XMLA offered without the data APIs")
	}
}

func TestXMLAOnInTheInstallation(t *testing.T) {
	f := newDapiFixture(t)
	f.xmlaOn()
	limits := decode[struct {
		Xmla xmlaLimitsOut `json:"xmla"`
	}](t, f.call(f.viewer, dataAPILimits, http.MethodGet, nil, nil))
	if limits.Xmla != (xmlaLimitsOut{Enabled: true, MaxCells: 300_000, MaxTuples: 4000, MaxMembers: 900, KeepRefused: true}) {
		t.Errorf("%+v", limits.Xmla)
	}
	admin := decode[struct {
		Xmla xmlaLimitsOut `json:"xmla"`
	}](t, f.call(f.admin, adminListDataAPIsRoute, http.MethodGet, nil, nil))
	if !admin.Xmla.Enabled || admin.Xmla.MaxMembers != 900 {
		t.Errorf("the administration: %+v", admin.Xmla)
	}

	orders := f.resource("orders", f.orders)
	orders["cube"] = "Orders of the CRM"
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "Pivot", "slug": f.slug("pv"), "xmla": map[string]any{"enabled": true},
		"resources": []any{f.resource("customers", f.customers), orders}}, http.StatusCreated)
	path := map[string]string{"api_id": dapiID(a.ID)}
	if !a.Xmla.Enabled || a.Resources[0].Cube != "" || a.Resources[1].Cube != "Orders of the CRM" {
		t.Fatalf("created: %+v", a)
	}
	// what was saved is what is read back (no column of its own: the version's JSON)
	again := decode[dapiDetail](t, f.call(f.viewer, getDataAPI, http.MethodGet, nil, path))
	if !again.Xmla.Enabled || again.Resources[1].Cube != "Orders of the CRM" || again.CurrentVersion != 1 {
		t.Fatalf("read back: %+v", again)
	}
	// two cubes of one name
	clash := f.resource("orders", f.orders)
	clash["cube"] = "Customers"
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"resources": []any{f.resource("customers", f.customers), clash}}, path); w.Code != http.StatusUnprocessableEntity ||
		!strings.Contains(w.Body.String(), "both the cube") {
		t.Errorf("%d %s", w.Code, w.Body.String())
	}
	// a change of name alone is no version, and keeps the switch
	if v := decode[dapiDetail](t, f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"name": "Pivot 2"}, path)); !v.Xmla.Enabled || v.CurrentVersion != 1 {
		t.Errorf("%+v", v)
	}
	// the switch alone is a version, and the captions stay
	v2 := decode[dapiDetail](t, f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"xmla": map[string]any{"enabled": false}}, path))
	if v2.Xmla.Enabled || v2.CurrentVersion != 2 || v2.Resources[1].Cube != "Orders of the CRM" {
		t.Fatalf("switched off: %+v", v2)
	}
	// a new caption, with the resources, is a version
	renamed := f.resource("orders", f.orders)
	renamed["cube"] = "Sales orders"
	v3 := decode[dapiDetail](t, f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"xmla": map[string]any{"enabled": true},
		"resources": []any{f.resource("customers", f.customers), renamed}}, path))
	if !v3.Xmla.Enabled || v3.CurrentVersion != 3 || v3.Resources[1].Cube != "Sales orders" {
		t.Fatalf("%+v", v3)
	}
	// a version restored brings its switch and its captions back
	r := decode[dapiDetail](t, f.call(f.creator, restoreDataAPIVersion, http.MethodPost, nil, map[string]string{"api_id": dapiID(a.ID), "version": "2"}))
	if r.Xmla.Enabled || r.CurrentVersion != 4 || r.Resources[1].Cube != "Orders of the CRM" {
		t.Fatalf("restored: %+v", r)
	}
	r = decode[dapiDetail](t, f.call(f.creator, restoreDataAPIVersion, http.MethodPost, nil, map[string]string{"api_id": dapiID(a.ID), "version": "3"}))
	if !r.Xmla.Enabled || r.Resources[1].Cube != "Sales orders" {
		t.Fatalf("restored: %+v", r)
	}
	// the version's own content is the JSON with both fields
	var content string
	if err := f.db.QueryRow(f.ctx, `SELECT content FROM data_api_versions WHERE data_api_id = $1 AND version = 3`, a.ID).Scan(&content); err != nil ||
		!strings.Contains(content, `"xmla":{"enabled":true}`) || !strings.Contains(content, `"cube":"Sales orders"`) {
		t.Errorf("%v %s", err, content)
	}
	// a copy keeps both
	cp := decode[dapiDetail](t, f.call(f.creator, duplicateDataAPI, http.MethodPost, nil, path))
	if !cp.Xmla.Enabled || cp.Resources[1].Cube != "Sales orders" {
		t.Errorf("copy: %+v", cp)
	}
	// the installation's switch turned off later: the copy of one that has it on is made with it off
	f.d.Config.DataAPI.XMLA.Enabled = false
	cp2 := decode[dapiDetail](t, f.call(f.creator, duplicateDataAPI, http.MethodPost, nil, path))
	if cp2.Xmla.Enabled || cp2.Resources[1].Cube != "Sales orders" {
		t.Errorf("copy with the switch off: %+v", cp2)
	}
	// and the original, saved again with it on, is refused
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"resources": []any{f.resource("customers", f.customers), renamed}}, path); w.Code != http.StatusConflict {
		t.Errorf("%d %s", w.Code, w.Body.String())
	}
}

func TestXMLAInTheManifest(t *testing.T) {
	f := newDapiFixture(t)
	dapiFakeEngine(t, f)
	_, _ = f.db.Exec(f.ctx, `UPDATE datasources SET bucket = 'tabularia', key = 'datasets/customers.parquet' WHERE id = $1`, f.customers)
	f.xmlaOn()
	orders := f.resource("orders", f.orders)
	orders["cube"] = "Sales orders"
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "Pivot", "slug": f.slug("pv"), "xmla": map[string]any{"enabled": true},
		"resources": []any{f.resource("customers", f.customers), orders}}, http.StatusCreated)
	path := map[string]string{"api_id": dapiID(a.ID)}
	if w := f.call(f.creator, activateDataAPI, http.MethodPost, nil, path); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	token, _ := dataapis.RotatePodToken(f.ctx, f.db, a.ID)
	m := f.manifest(token, a.ID)
	if m.XMLA == nil || *m.XMLA != (dataapiwire.XMLA{MaxCells: 300_000, MaxTuples: 4000, MaxMembers: 900, KeepRefused: true}) {
		t.Fatalf("%+v", m.XMLA)
	}
	if len(m.Resources) != 2 || m.Resources[0].Cube != "customers" || m.Resources[1].Cube != "Sales orders" {
		t.Errorf("cubes: %+v", m.Resources)
	}
	// the caps are the administrator's, read at every manifest
	f.d.Config.DataAPI.XMLA.MaxCells, f.d.Config.DataAPI.XMLA.KeepRefused = 5000, false
	if m = f.manifest(token, a.ID); m.XMLA == nil || m.XMLA.MaxCells != 5000 || m.XMLA.KeepRefused {
		t.Errorf("%+v", m.XMLA)
	}
	// the installation's switch off: the pod is told nothing of XMLA, so it answers 404
	f.d.Config.DataAPI.XMLA.Enabled = false
	if m = f.manifest(token, a.ID); m.XMLA != nil {
		t.Errorf("%+v", m.XMLA)
	}
	f.d.Config.DataAPI.XMLA.Enabled = true
	// the data API's off, too
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"xmla": map[string]any{"enabled": false}}, path); w.Code != http.StatusOK {
		t.Fatal(w.Body.String())
	}
	if m = f.manifest(token, a.ID); m.XMLA != nil {
		t.Errorf("%+v", m.XMLA)
	}
	// a data API that has not got the field at all (a version of before) is off
	_, _ = f.db.Exec(f.ctx, `UPDATE data_api_versions SET content = (content::jsonb - 'xmla')::text WHERE data_api_id = $1`, a.ID)
	if m = f.manifest(token, a.ID); m.XMLA != nil || m.Resources[1].Cube != "Sales orders" {
		t.Errorf("%+v", m)
	}
}
