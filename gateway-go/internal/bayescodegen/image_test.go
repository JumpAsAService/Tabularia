//go:build bayesimage

package bayescodegen

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The measure of lot S5's acceptance, on the image (docs/specs/bayes-stan-codegen.md, «As built — lot
// S5»): the 747-node program of the expression language's E4 measures, written by Generate, builds
// with CmdStan in the image with 1 CPU and 3 GiB in less than 90 s (60.7 s measured; more than 330 s
// without the splitting). A full CmdStan build, so it is kept out of the normal run by a build tag:
//
//	BAYES_IMAGE=tabularia-bayes-b0:local go test -tags bayesimage -count=1 -run TestImageBuild747 -v ./internal/bayescodegen/
//
// One heavy job at a time on a small machine. BAYES_IMAGE defaults to tabularia-bayes-b0:local.

// splitGraph is the graph of the measure: `terms` of the chain a * x / (x + b) - … + 1 (8 nodes a
// term and one) in each of len(terms) transformed nodes over the rows, summed in the mean.
func splitGraph(terms ...int) []byte {
	slot := func(s string) map[string]any { return map[string]any{"expr": s} }
	norm := func(mu, sigma string) map[string]any {
		return map[string]any{"dist": "normal", "args": map[string]any{"mu": slot(mu), "sigma": slot(sigma)}}
	}
	nodes := []any{
		map[string]any{"id": "y", "kind": "data", "name": "y", "label": "Y", "plate": "rows", "type": "real"},
		map[string]any{"id": "x", "kind": "data", "name": "x", "label": "X", "plate": "rows", "type": "real"},
		map[string]any{"id": "a", "kind": "parameter", "name": "a", "label": "A", "prior": norm("0", "1")},
		map[string]any{"id": "b", "kind": "parameter", "name": "b", "label": "B", "prior": norm("0", "1")},
		map[string]any{"id": "s", "kind": "parameter", "name": "s", "label": "S", "prior": map[string]any{"dist": "exponential", "args": map[string]any{"beta": slot("1")}}},
	}
	var ts []string
	for i, n := range terms {
		id := fmt.Sprintf("t%d", i+1)
		ts = append(ts, id)
		chain := strings.TrimSuffix(strings.Repeat("a * x / (x + b) - ", n), " - ") + " + 1"
		nodes = append(nodes, map[string]any{"id": id, "kind": "transformed", "name": id, "label": id, "plate": "rows", "value": slot(chain)})
	}
	nodes = append(nodes, map[string]any{"id": "lik", "kind": "likelihood", "label": "L", "observed": "y", "dist": "normal",
		"args": map[string]any{"mu": slot(strings.Join(ts, " + ")), "sigma": slot("s")}})
	data, err := json.Marshal(map[string]any{"format_version": 1, "plates": []any{map[string]any{"id": "rows", "kind": "rows", "label": "Rows"}}, "nodes": nodes})
	if err != nil {
		panic(err)
	}
	return data
}

func TestImageBuild747(t *testing.T) {
	image := os.Getenv("BAYES_IMAGE")
	if image == "" {
		image = "tabularia-bayes-b0:local"
	}
	if out, err := exec.Command("docker", "image", "inspect", "--format", "{{.Id}}", image).CombinedOutput(); err != nil {
		t.Skipf("the image %s is not here (%v: %s)", image, err, strings.TrimSpace(string(out)))
	}
	m, ok := validModel(splitGraph(31, 31, 31))
	if !ok {
		t.Fatal("the graph of the measure is not a valid model")
	}
	p, err := Generate(m, nil)
	if err != nil {
		t.Fatal(err)
	}
	// 31 terms of 8 nodes and one are 249 nodes a transformed node: four locals each
	if n := strings.Count(string(p.Text), " E_t1_"); n < 4 {
		t.Fatalf("the program holds %d uses of E_t1_, want the 4 locals of 249 nodes:\n%s", n, p.Text)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "model.stan"), p.Text, 0o644); err != nil {
		t.Fatal(err)
	}
	// the build the compile Job runs: nothing writable but /tmp, no network, 1 CPU, 3 GiB
	script := "cd /opt/cmdstan && mkdir -p /tmp/b && cp /w/model.stan /tmp/b/model.stan && make /tmp/b/model > /tmp/log 2>&1; rc=$?; " +
		"[ $rc -ne 0 ] && grep -m3 -i error /tmp/log | cut -c1-200; exit $rc"
	ctx, cancel := context.WithTimeout(context.Background(), 330*time.Second)
	defer cancel()
	var out bytes.Buffer
	cmd := exec.CommandContext(ctx, "docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,exec",
		"--user", "10001:10001", "--memory", "3g", "--memory-swap", "3g", "--cpus", "1",
		"-v", dir+":/w:ro", "--entrypoint", "sh", image, "-c", script)
	cmd.Stdout, cmd.Stderr = &out, &out
	start := time.Now()
	err = cmd.Run()
	took := time.Since(start)
	if err != nil {
		t.Fatalf("the build failed after %s: %v\n%s", took.Round(time.Second), err, out.String())
	}
	t.Logf("the 747-node program built in %s", took.Round(100*time.Millisecond))
	if took > 90*time.Second {
		t.Errorf("the build took %s, lot S5's acceptance is less than 90 s", took.Round(time.Second))
	}
}
