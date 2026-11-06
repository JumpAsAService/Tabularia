package api

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func TestRunHelpers(t *testing.T) {
	if got := friendlyError(strp("boom"), strp("WorkerLostError: SIGKILL")); *got != oomMessage {
		t.Errorf("oom: %s", *got)
	}
	if got := friendlyError(strp("PostgreSQL: out of memory"), nil); *got != "PostgreSQL: out of memory" {
		t.Errorf("database message: %s", *got)
	}
	if got := emailNotSent(strp(`{"to": ["a@x.it"], "ok": null}`)); got == nil ||
		*got != `{"to": ["a@x.it"], "ok": false, "error": "not sent: the run did not succeed"}` {
		t.Errorf("email not sent: %v", got)
	}
	if emailNotSent(strp(`{"ok": true}`)) != nil || emailNotSent(strp(`[1]`)) != nil {
		t.Error("email already told")
	}
	out, _ := pyjson.Loads(`{"path": "/o/a.csv", "bytes": 3}`)
	if got := sftpWritten(strp(`{"type": "sftp", "file_name": "a.csv"}`), out); got == nil ||
		*got != `{"type": "sftp", "file_name": "a.csv", "path": "/o/a.csv", "bytes": 3}` {
		t.Errorf("sftp written: %v", got)
	}
	res, _ := pyjson.Loads(`{"missing": [1, 2], "duplicate_keys": 4}`)
	if got := notesOf(res.(pyjson.Object)); got == nil || *got != `{"missing": [1, 2], "missing_count": 2, "expansions": null, "duplicate_keys": 4}` {
		t.Errorf("notes: %v", got)
	}
	if got := emailSent(strp(`{"to": ["a"], "ok": null}`), pyjson.Object{{Key: "ok", Value: true}}); got != `{"to": ["a"], "ok": true}` {
		t.Errorf("email sent: %s", got)
	}
}

func TestParseDay(t *testing.T) {
	for in, ok := range map[string]bool{"2026-01-05": true, "2026-1-5": true, "2026-02-30": false, "26-01-05": false, "2026-01- 5": true, "2026-13-01": false} {
		if _, got := parseDay(in); got != ok {
			t.Errorf("%q: %v", in, got)
		}
	}
}

func TestGroupDefinition(t *testing.T) {
	if g := definitionOf("not json"); len(g.steps) != 0 || g.dumpJSON() != `{"steps":[]}` {
		t.Errorf("unreadable: %s", g.dumpJSON())
	}
	g := definitionOf(`{"steps": [[{"kind": "flow", "id": 3}, null]]}`)
	if len(g.cells()) != 1 || g.dumpJSON() != `{"steps":[[{"kind":"flow","id":3,"container":null,"pushmail":null},null]]}` {
		t.Errorf("dump: %s", g.dumpJSON())
	}
	if !sqlOutputOf(`{"output": {"name": "x", "project_id": 2}}`) || sqlOutputOf(`{"output": {"name": "", "project_id": 2}}`) {
		t.Error("sql output")
	}
}

func TestGroupBodyDefinition(t *testing.T) {
	var ps pyd.Params
	b, _ := pyd.ReadBody([]byte(`{"name": "g", "definition": {"steps": [[{"kind": "flow", "id": 2}]]}}`), "application/json")
	mv := ps.Body(b, gatewaymodels.FlowGroupCreate)
	g, errs := newGroupDef(asInput(mv.Get("definition")))
	if errs != nil || len(g.cells()) != 1 {
		t.Fatalf("%v %v", g, errs)
	}
	b, _ = pyd.ReadBody([]byte(`{"name": "g"}`), "application/json")
	mv = ps.Body(b, gatewaymodels.FlowGroupCreate)
	if g, errs := newGroupDef(asInput(mv.Get("definition"))); errs != nil || len(g.steps) != 0 {
		t.Fatalf("default: %v %v", g, errs)
	}
}
