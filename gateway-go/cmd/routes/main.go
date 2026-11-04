// routes prints the routes the Go gateway answers, as JSON [[method, pattern], …], read from
// the registry itself (scripts/inventory.sh): no pattern matching on the source.
package main

import (
	"encoding/json"
	"io"
	"log/slog"
	"os"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

func main() {
	// the handlers are built, never called: empty dependencies are enough
	d := &api.Deps{Config: &config.Config{}, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	out := [][2]string{{"GET", "/health"}}
	for _, r := range api.Routes(d) {
		out = append(out, [2]string{r.Method, r.Pattern})
	}
	json.NewEncoder(os.Stdout).Encode(out)
}
