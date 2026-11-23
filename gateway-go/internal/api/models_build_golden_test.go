package api

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// mgGoldenFile holds, for every group shipped before the group `bayes`, the sha256 of what the build loop makes for it: the
// Dockerfile (scriptBuilds.dockerfile, the gateway's own path, with the recipe's packages) and the check Job's script. A change of
// the machinery for a new group (docs/specs/bayes-image.md, «Changes to the machinery») leaves them as they were; one meant to
// change them rewrites the file:
//
//	TABULARIA_UPDATE_GOLDEN=1 go test ./internal/api/ -run TestTheOtherGroupsBuildAndCheckAreAsTheyWere
const mgGoldenFile = "testdata/model_groups.golden.json"

var mgGoldenGroups = []string{"regression", "survival", "timeseries", "boosting", "customer_lifetime", "experiments", "foundation_forecast"}

func TestTheOtherGroupsBuildAndCheckAreAsTheyWere(t *testing.T) {
	// the configuration's defaults, with a key of zeros (the Dockerfile and the script read no secret)
	zeros := base64.URLEncoding.EncodeToString(make([]byte, 32))
	cfg, err := config.Load(func(name string) (string, bool) { return zeros, name == "SECURITY__FERNET_KEY" })
	if err != nil {
		t.Fatal(err)
	}
	b := &scriptBuilds{log: slog.Default(), panel: settings.New(cfg, false, slog.Default()), cfg: cfg.Jobs.Kube,
		s: scriptcfg.Load(func(string) (string, bool) { return "", false })}
	images, err := b.s.Images()
	if err != nil {
		t.Fatal(err)
	}
	digest := func(s string) string {
		h := sha256.Sum256([]byte(s))
		return hex.EncodeToString(h[:])
	}
	got := map[string]map[string]string{}
	for _, id := range mgGoldenGroups {
		g, ok := modelgroups.Get(id)
		if !ok {
			t.Fatalf("no group %s", id)
		}
		base, err := modelgroups.BaseImage(g.Recipe, images)
		if err != nil {
			t.Fatal(err)
		}
		managed := mgManagedPrefix + id
		d, err := b.dockerfile(g.Recipe.Language, base, g.Recipe.Packages, "", &managed)
		if err != nil {
			t.Fatal(err)
		}
		sized := ""
		if g.Recipe.Weights != nil {
			if sized, err = b.dockerfile(g.Recipe.Language, base, g.Recipe.Packages, "# tabularia:weights 5m\n", &managed); err != nil {
				t.Fatal(err)
			}
		}
		got[id] = map[string]string{"dockerfile_sha256": digest(d), "dockerfile_5m_sha256": digest(sized),
			"check_script_sha256": digest(mgCheckScriptOf(id, g.Recipe.Language))}
	}
	text, err := json.MarshalIndent(got, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	text = append(text, '\n')
	if os.Getenv("TABULARIA_UPDATE_GOLDEN") == "1" {
		if err := os.MkdirAll(filepath.Dir(mgGoldenFile), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(mgGoldenFile, text, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(mgGoldenFile)
	if err != nil {
		t.Fatalf("%s: %v", mgGoldenFile, err)
	}
	if !bytes.Equal(want, text) {
		t.Errorf("the build or the check of a group changed:\nbefore\n%s\nnow\n%s", want, text)
	}
}
