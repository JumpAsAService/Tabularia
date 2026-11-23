package modelgroups

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
)

// goldenFile holds, for every group shipped before the group `bayes`, the bytes the machinery makes for it: the sha256 of its
// Dockerfile (the official hosts, and the mirrors), of the ConfigMap of a run and of the self-test, and its three hashes. A
// change of the machinery for a new group (docs/specs/bayes-image.md, «Changes to the machinery») leaves every one of them as
// it was: a new Dockerfile, a new runner hash or a new ConfigMap would build or recheck an image that did not change. A
// change that is meant to change a group's bytes rewrites the file:
//
//	TABULARIA_UPDATE_GOLDEN=1 go test ./internal/modelgroups/ -run TestTheOtherGroupsBytesAreAsTheyWere
const goldenFile = "testdata/groups.golden.json"

type goldenGroup struct {
	RecipeHash         string `json:"recipe_hash"`
	RunnerHash         string `json:"runner_hash"`
	ReferenceHash      string `json:"reference_hash"`
	Dockerfile         string `json:"dockerfile_sha256"`
	DockerfileMirrors  string `json:"dockerfile_mirrors_sha256"`
	ConfigMapRun       string `json:"configmap_run_sha256"`
	ConfigMapSelftest  string `json:"configmap_selftest_sha256"`
	DockerfileByteSize int    `json:"dockerfile_bytes"`
}

// goldenGroups are the groups the file covers: the seven shipped before `bayes`.
var goldenGroups = []string{"regression", "survival", "timeseries", "boosting", "customer_lifetime", "experiments", "foundation_forecast"}

func sum(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// configMapBytes is a ConfigMap's data, binaryData and items as one JSON text (map keys sorted by encoding/json).
func configMapBytes(t *testing.T, code []File) []byte {
	t.Helper()
	data, binary, items := ConfigMap(code)
	b, err := json.Marshal(map[string]any{"data": data, "binaryData": binary, "items": items})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func goldenOf(t *testing.T, id string) goldenGroup {
	t.Helper()
	g, ok := Get(id)
	if !ok {
		t.Fatalf("no group %s", id)
	}
	s := scriptimage.Settings{CRANURL: "https://cran.r-project.org", Jobs: 2}
	d, err := DockerfileFor(id, scriptcfg.DefaultBaseImages, s, Build{})
	if err != nil {
		t.Fatal(err)
	}
	mirrors := s
	mirrors.PyPIIndexURL = "https://pypi.mirror.example/simple"
	dm, err := DockerfileFor(id, scriptcfg.DefaultBaseImages, mirrors, Build{WheelsBaseURL: "https://wheels.mirror.example/torch",
		WeightsBaseURL: "https://hub.mirror.example/"})
	if err != nil {
		t.Fatal(err)
	}
	return goldenGroup{RecipeHash: g.RecipeHash, RunnerHash: g.RunnerHash, ReferenceHash: g.ReferenceHash,
		Dockerfile: sum([]byte(d)), DockerfileMirrors: sum([]byte(dm)), DockerfileByteSize: len(d),
		ConfigMapRun:      sum(configMapBytes(t, mustCode(t, id, []byte(`{"protocol":1}`), false))),
		ConfigMapSelftest: sum(configMapBytes(t, mustCode(t, id, nil, true)))}
}

func TestTheOtherGroupsBytesAreAsTheyWere(t *testing.T) {
	got := map[string]goldenGroup{}
	for _, id := range goldenGroups {
		got[id] = goldenOf(t, id)
	}
	text, err := json.MarshalIndent(got, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	text = append(text, '\n')
	if os.Getenv("TABULARIA_UPDATE_GOLDEN") == "1" {
		if err := os.MkdirAll(filepath.Dir(goldenFile), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(goldenFile, text, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(goldenFile)
	if err != nil {
		t.Fatalf("%s: %v", goldenFile, err)
	}
	if bytes.Equal(want, text) {
		return
	}
	var before map[string]goldenGroup
	if err := json.Unmarshal(want, &before); err != nil {
		t.Fatalf("%s: %v", goldenFile, err)
	}
	for _, id := range goldenGroups {
		if before[id] != got[id] {
			t.Errorf("%s: the bytes the machinery makes changed:\n  before %+v\n  now    %+v", id, before[id], got[id])
		}
	}
	if len(before) != len(got) {
		t.Errorf("%s lists %d groups, the test %d", goldenFile, len(before), len(got))
	}
}
