// Command modelgroup builds and checks a statistical model group's image on a laptop,
// without a cluster, exactly as the cluster builds and checks it
// (docs/specs/statistical-models.md, «Commands»):
//
//	go run ./cmd/modelgroup dockerfile regression > /tmp/regression.Dockerfile
//	docker build -f /tmp/regression.Dockerfile -t tabularia-models-regression:dev /tmp
//	go run ./cmd/modelgroup code regression /tmp/regression-code
//	docker run --rm -v /tmp/regression-code:/tabularia/code:ro tabularia-models-regression:dev \
//	    Rscript /tabularia/code/runner/run.R --selftest
//
// A Python group (boosting) the same way; its image is a download of wheels (nothing compiles),
// and its runner is started as the Job starts it, on a read-only root with /tmp writable:
//
//	go run ./cmd/modelgroup dockerfile boosting > /tmp/boosting.Dockerfile
//	docker build -f /tmp/boosting.Dockerfile -t tabularia-models-boosting:dev /tmp
//	go run ./cmd/modelgroup code boosting /tmp/boosting-code
//	docker run --rm --read-only --tmpfs /tmp -v /tmp/boosting-code:/tabularia/code:ro tabularia-models-boosting:dev \
//	    python -I -B /tabularia/code/runner/run.py --selftest
//
// Subcommands:
//
//	dockerfile <group>                   the image's Dockerfile (base image from SCRIPTS__BASE_IMAGES or
//	                                     Tabularia's defaults, CRAN from SCRIPTS__CRAN_URL, PyPI from
//	                                     SCRIPTS__PYPI_INDEX_URL; -jobs compilers)
//	code <group> <dir> [<task.json>]     writes the Job's /tabularia/code into <dir>: the runner, the common
//	                                     library, the schema, the catalogue, the reference cases, the task
//	task <model> <params.json>           the task.json of a fit with those parameters (checked by the catalogue)
//	groups                               each group's recipe hash, runner hash and reference hash
//
// The directory written by `code` is what the Job's ConfigMap holds; a laptop with R (and
// jsonlite) also runs an R group's self-test without Docker:
//
//	TABULARIA_SELFTEST_NO_ARROW=1 Rscript /tmp/regression-code/runner/run.R --selftest
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
)

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "modelgroup:", err)
		os.Exit(1)
	}
}

func run(args []string, out io.Writer) error {
	fl := flag.NewFlagSet("modelgroup", flag.ContinueOnError)
	jobs := fl.Int64("jobs", 2, "compilers at once in the image's build")
	if err := fl.Parse(args); err != nil {
		return err
	}
	args = fl.Args()
	if len(args) == 0 {
		return errors.New("usage: modelgroup [-jobs n] dockerfile <group> | code <group> <dir> [<task.json>] | task <model> <params.json> | groups")
	}
	switch args[0] {
	case "dockerfile":
		if len(args) != 2 {
			return errors.New("usage: modelgroup dockerfile <group>")
		}
		s := scriptcfg.Load(config.FromOS())
		images, err := s.Images()
		if err != nil {
			return err
		}
		d, err := modelgroups.Dockerfile(args[1], images, scriptimage.Settings{
			PyPIIndexURL: s.Scripts.PyPIIndexURL, CRANURL: s.Scripts.CRANURL, Jobs: *jobs})
		if err != nil {
			return err
		}
		_, err = io.WriteString(out, d)
		return err
	case "code":
		if len(args) != 3 && len(args) != 4 {
			return errors.New("usage: modelgroup code <group> <dir> [<task.json>]")
		}
		var task []byte
		if len(args) == 4 {
			b, err := os.ReadFile(args[3])
			if err != nil {
				return err
			}
			task = b
		}
		code, err := modelgroups.CodeFiles(args[1], task, true)
		if err != nil {
			return err
		}
		for _, f := range code {
			path := filepath.Join(args[2], filepath.FromSlash(f.Path))
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				return err
			}
			if err := os.WriteFile(path, f.Content, 0o644); err != nil {
				return err
			}
		}
		_, err = fmt.Fprintf(out, "%d files written in %s\n", len(code), args[2])
		return err
	case "task":
		if len(args) != 3 {
			return errors.New("usage: modelgroup task <model> <params.json>")
		}
		b, err := os.ReadFile(args[2])
		if err != nil {
			return err
		}
		var params map[string]any
		if err := json.Unmarshal(b, &params); err != nil {
			return fmt.Errorf("%s: %v", args[2], err)
		}
		t, err := modelgroups.FitTask(args[1], params, false, 0)
		if err != nil {
			return err
		}
		t.RowsFrom = "trial"
		_, err = fmt.Fprintf(out, "%s\n", t.JSON())
		return err
	case "groups":
		for _, g := range modelgroups.Groups() {
			if _, err := fmt.Fprintf(out, "%s\trecipe %s\trunner %s\treference %s\t%d packages\n", g.ID, g.RecipeHash[:12],
				g.RunnerHash[:12], g.ReferenceHash[:12], len(g.Recipe.Packages)); err != nil {
				return err
			}
		}
		return nil
	}
	return fmt.Errorf("unknown subcommand %q", args[0])
}
