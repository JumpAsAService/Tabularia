package bayestemplates

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

const referenceDir = "testdata/reference/"

// compareFile is the last record of lot T3's comparison (docs/specs/bayes-templates.md, «Reference values»).
type compareFile struct {
	Header struct {
		Alpha       float64 `json:"alpha"`
		Correction  string  `json:"correction"`
		Comparisons int     `json:"comparisons"`
		Cmdstan     string  `json:"cmdstan"`
	} `json:"header"`
	Records []struct {
		Template        string  `json:"template"`
		TemplateVersion int     `json:"template_version"`
		Compared        int     `json:"compared"`
		MinPHolm        float64 `json:"min_p_holm"`
		Passed          bool    `json:"passed"`
		TabProgram      string  `json:"tabularia_program_sha256"`
	} `json:"records"`
}

// TestReferenceValuesAreRecordedForTheCurrentVersions is the CI side of lot T3: the heavy runs are by hand
// (one at a time, in the group's image), CI only checks that the reference data, the hand-written program,
// <id>.expected.json and the comparison's last record exist, name the template's CURRENT version, and that the
// program Tabularia generated then is the one it generates now. A template whose `version` changes (or whose
// program changes) without new reference values fails here.
func TestReferenceValuesAreRecordedForTheCurrentVersions(t *testing.T) {
	var cmp compareFile
	raw, err := os.ReadFile(referenceDir + "compare.json")
	if err != nil {
		t.Fatalf("compare.json: %v (record the reference values: testdata/reference/run_reference.py and compare.py)", err)
	}
	if err := json.Unmarshal(raw, &cmp); err != nil {
		t.Fatalf("compare.json: %v", err)
	}
	if cmp.Header.Alpha != 0.001 || cmp.Header.Correction != "holm" || cmp.Header.Cmdstan != "2.40.0" {
		t.Errorf("compare.json header = %+v, want alpha 0.001, holm, CmdStan 2.40.0", cmp.Header)
	}
	last := map[string]int{}
	for i, r := range cmp.Records {
		last[r.Template] = i
	}
	for _, tpl := range List() {
		i, ok := last[tpl.ID]
		if !ok {
			t.Errorf("%s: no record in compare.json", tpl.ID)
			continue
		}
		r := cmp.Records[i]
		if r.TemplateVersion != tpl.Version {
			t.Errorf("%s: the reference values are for version %d, the template is at %d: record new ones", tpl.ID, r.TemplateVersion, tpl.Version)
		}
		if !r.Passed || r.MinPHolm < cmp.Header.Alpha || r.Compared == 0 {
			t.Errorf("%s: the comparison did not pass (compared %d, min Holm p %g, alpha %g)", tpl.ID, r.Compared, r.MinPHolm, cmp.Header.Alpha)
		}
		golden, err := os.ReadFile(goldenDir + goldenCase(tpl) + ".stan")
		if err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(golden)
		if hex.EncodeToString(sum[:]) != r.TabProgram {
			t.Errorf("%s: the program the template generates is not the one the reference values compared", tpl.ID)
		}
		var exp struct {
			Header struct {
				Template string `json:"template"`
				Version  int    `json:"template_version"`
				Cmdstan  string `json:"cmdstan"`
			} `json:"header"`
			Quantities map[string]map[string]float64 `json:"quantities"`
		}
		raw, err := os.ReadFile(referenceDir + tpl.ID + ".expected.json")
		if err != nil {
			t.Errorf("%s: %v", tpl.ID, err)
			continue
		}
		if err := json.Unmarshal(raw, &exp); err != nil {
			t.Errorf("%s.expected.json: %v", tpl.ID, err)
			continue
		}
		if exp.Header.Template != tpl.ID || exp.Header.Version != tpl.Version || exp.Header.Cmdstan != "2.40.0" || len(exp.Quantities) == 0 {
			t.Errorf("%s.expected.json header %+v (%d quantities) does not name the template at version %d", tpl.ID, exp.Header, len(exp.Quantities), tpl.Version)
		}
		for q, s := range exp.Quantities {
			if _, ok := s["mean"]; !ok {
				t.Errorf("%s: quantity %s has no mean", tpl.ID, q)
			}
		}
		csv, err := os.ReadFile(referenceDir + tpl.ID + ".csv")
		if err != nil {
			t.Errorf("%s: %v", tpl.ID, err)
		} else if rows := bytes.Count(csv, []byte("\n")) - 1; rows < 1 || rows > 2000 {
			t.Errorf("%s.csv has %d rows, want 1 to 2000", tpl.ID, rows)
		}
		if st, err := os.Stat(filepath.Join(referenceDir, tpl.ID+".stan")); err != nil || st.Size() == 0 {
			t.Errorf("%s: the hand-written program is missing", tpl.ID)
		}
	}
	if len(last) != len(List()) {
		t.Errorf("compare.json holds records for %d templates, the file has %d", len(last), len(List()))
	}
}
