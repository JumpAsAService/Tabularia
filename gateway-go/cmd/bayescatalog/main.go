// Command bayescatalog writes the snapshot of the Bayesian editor's distribution catalogue
// (docs/specs/bayes-distribution-catalog.md, «Commands») from the declarations of
// internal/bayescatalog. Run it from gateway-go/ whenever a declaration changes, then copy the
// snapshot with .github/scripts/bayes_catalog.py:
//
//	go run ./cmd/bayescatalog
//	python3 ../.github/scripts/bayes_catalog.py
//
// -o names another file (the default is the snapshot the gateway embeds).
package main

import (
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

const defaultOutput = "internal/bayescatalog/catalog.json"

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "bayescatalog:", err)
		os.Exit(1)
	}
}

func run(args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("bayescatalog", flag.ContinueOnError)
	out := fs.String("o", defaultOutput, "the file to write")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("no argument is expected, got %q", fs.Arg(0))
	}
	snapshot := bayescatalog.Build()
	if err := os.WriteFile(*out, snapshot, 0o644); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "%s written, %d bytes, %d distributions\n", *out, len(snapshot), len(bayescatalog.Declared().Distributions))
	return nil
}
