package main

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// The image is built from a context that holds only what the Dockerfile copies. A package of the
// gateway this program starts to import, and the Dockerfile does not copy, builds everywhere but
// in the image: `go test` passes and the release fails. This reads the imports and the COPY lines.
func TestTheDockerfileCopiesEveryGatewayPackageTheProgramImports(t *testing.T) {
	const prefix = "github.com/JumpAsAService/Tabularia/gateway-go/internal/"
	imported := regexp.MustCompile(`"` + regexp.QuoteMeta(prefix) + `([a-z0-9_]+)`)

	// the packages of the gateway a tree of Go files imports, tests left out (the image has none)
	importsOf := func(dir string) []string {
		var found []string
		err := filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
			if err != nil || d.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
				return err
			}
			text, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			for _, m := range imported.FindAllStringSubmatch(string(text), -1) {
				found = append(found, m[1])
			}
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
		return found
	}

	// what this program imports, and what those packages import in their turn
	needed := map[string]bool{}
	queue := importsOf(".")
	for len(queue) > 0 {
		name := queue[0]
		queue = queue[1:]
		if needed[name] {
			continue
		}
		needed[name] = true
		queue = append(queue, importsOf(filepath.Join("..", "internal", name))...)
	}
	if len(needed) == 0 {
		t.Fatal("no gateway package found among the imports: the test reads nothing")
	}

	dockerfile, err := os.ReadFile("Dockerfile")
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for name := range needed {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if !strings.Contains(string(dockerfile), "COPY internal/"+name+" internal/"+name+"\n") {
			t.Errorf("the Dockerfile does not copy internal/%s, which the program imports", name)
		}
	}
}
