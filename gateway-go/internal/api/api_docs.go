package api

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/apidocs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// core/api_docs.py (/docs, /redoc), FastAPI's /openapi.json and main.py root (GET /).
func init() {
	register("api_docs", func(d *Deps) []server.Route {
		docs := apidocs.New(d.Config.AppName, d.Config.AppVersion, scriptcfg.Get().App.RootPath)
		name := d.Config.AppName
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/docs", Handler: http.HandlerFunc(docs.Swagger)},
			{Method: http.MethodGet, Pattern: "/redoc", Handler: http.HandlerFunc(docs.Redoc)},
			{Method: http.MethodGet, Pattern: "/openapi.json", Handler: http.HandlerFunc(docs.OpenAPI)},
			// main.py root; «/{$}» is the root alone (server.FastAPIPath)
			{Method: http.MethodGet, Pattern: "/{$}", Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				server.JSON(w, http.StatusOK, map[string]string{"message": name + " gateway is running"})
			})},
		}
	})
}
