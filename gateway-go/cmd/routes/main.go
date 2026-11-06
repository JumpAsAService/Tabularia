// routes prints the routes the Go gateway answers, as JSON [[method, pattern], …], read from
// the registry itself (scripts/inventory.sh): no pattern matching on the source. The routes it
// forwards to the AI service come as [method, path, "ai"], their path as FastAPI declares it.
package main

import (
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func main() {
	// the handlers are built, never called: empty dependencies are enough
	d := &api.Deps{Config: &config.Config{}, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	out := [][]string{{"GET", "/health"}}
	ai := api.AIServiceRoutes()
	forwarded := map[[2]string]bool{}
	for _, r := range ai {
		forwarded[[2]string{r[0], strings.Replace(r[1], ":path}", "...}", 1)}] = true
	}
	for _, r := range api.Routes(d) {
		if !forwarded[[2]string{r.Method, r.Pattern}] {
			out = append(out, []string{r.Method, server.FastAPIPath(r.Pattern)})
		}
	}
	for _, r := range ai {
		out = append(out, []string{r[0], r[1], "ai"})
	}
	json.NewEncoder(os.Stdout).Encode(out)
}
