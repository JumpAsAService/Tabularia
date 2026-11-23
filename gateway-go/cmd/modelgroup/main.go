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
//	dockerfile <group> [--sizes 5m,28m]  the image's Dockerfile (base image from SCRIPTS__BASE_IMAGES or
//	                                     Tabularia's defaults, CRAN from SCRIPTS__CRAN_URL, PyPI from
//	                                     SCRIPTS__PYPI_INDEX_URL; -jobs compilers). A group that downloads
//	                                     weights (foundation_forecast) takes the sizes to download, all of the
//	                                     recipe's when none is given, and the mirrors MODELS__WHEELS_BASE_URL and
//	                                     MODELS__WEIGHTS_BASE_URL (--wheels-base-url, --weights-base-url)
//	code <group> <dir> [<task.json>]     writes the Job's /tabularia/code into <dir>: the runner, the common
//	                                     library, the schema, the catalogue, the reference cases (gzipped, <path>.gz,
//	                                     as the check Job's ConfigMap holds them), the task
//	task <model> <params.json>           the task.json of a fit with those parameters (checked by the catalogue)
//	groups                               each group's recipe hash, runner hash and reference hash
//	pin <group> [--cran url] [--pypi url] the sha256 of every package of the recipe, downloaded and hashed here, written
//	                                     into its `hashes` (run from gateway-go/; --recipe names another file). R: each
//	                                     source tarball from CRAN, and its DESCRIPTION checked against the order of the
//	                                     recipe; Python: each wheel a Linux build may pick, from PyPI, checked against
//	                                     PyPI's digest. Run it whenever a version in the recipe changes
//
// The image of the pre-trained forecasting group is built the same way, with the weights of the sizes wanted (a download of
// 0.02 to 0.84 GB each, checked by sha256), and its self-test runs on a read-only root as the Job's does:
//
//	go run ./cmd/modelgroup dockerfile foundation_forecast --sizes 5m,28m > /tmp/ff.Dockerfile
//	docker build -f /tmp/ff.Dockerfile -t tabularia-models-foundation_forecast:dev /tmp
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
	"strings"

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
		return errors.New("usage: modelgroup [-jobs n] dockerfile <group> [--sizes 5m,28m] | code <group> <dir> [<task.json>] | task <model> <params.json> | groups | pin <group>")
	}
	switch args[0] {
	case "dockerfile":
		// `dockerfile <group> [--sizes …]`: the flags may follow the group's name, as they may lead
		df := flag.NewFlagSet("dockerfile", flag.ContinueOnError)
		sizes := df.String("sizes", "", "sizes of the weights to download, e.g. 5m,28m (every size of the recipe when empty)")
		wheelsBase := df.String("wheels-base-url", os.Getenv("MODELS__WHEELS_BASE_URL"), "mirror of the wheels' host (MODELS__WHEELS_BASE_URL)")
		weightsBase := df.String("weights-base-url", os.Getenv("MODELS__WEIGHTS_BASE_URL"), "mirror of the weights' host (MODELS__WEIGHTS_BASE_URL)")
		if err := df.Parse(args[1:]); err != nil {
			return err
		}
		rest := df.Args()
		if len(rest) == 0 {
			return errors.New("usage: modelgroup dockerfile <group> [--sizes 5m,28m]")
		}
		group := rest[0]
		if err := df.Parse(rest[1:]); err != nil || df.NArg() != 0 {
			return errors.New("usage: modelgroup dockerfile <group> [--sizes 5m,28m]")
		}
		build := modelgroups.Build{WheelsBaseURL: *wheelsBase, WeightsBaseURL: *weightsBase}
		for _, size := range strings.Split(*sizes, ",") {
			if size = strings.TrimSpace(size); size != "" {
				build.Sizes = append(build.Sizes, strings.ToLower(size))
			}
		}
		s := scriptcfg.Load(config.FromOS())
		images, err := s.Images()
		if err != nil {
			return err
		}
		d, err := modelgroups.DockerfileFor(group, images, scriptimage.Settings{
			PyPIIndexURL: s.Scripts.PyPIIndexURL, CRANURL: s.Scripts.CRANURL, Jobs: *jobs}, build)
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
	case "pin":
		return pinCommand(args[1:], out)
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
