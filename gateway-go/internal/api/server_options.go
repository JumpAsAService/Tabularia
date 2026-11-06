package api

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// ServerOptions are the framework's settings for one of the Python gateway's applications
// (app/main.py; the notebooks' host of routes/notebook_proxy.py install): its CORS origins,
// its root path, the scheme uvicorn believes from a trusted proxy.
func ServerOptions(d *Deps, app server.App) server.Options {
	opt := server.Options{App: app, RootPath: scriptcfg.Get().App.RootPath, Log: d.Log}
	if app == server.MainApp && d.Config != nil {
		opt.CORSOrigins = d.Config.Admin.CORSOrigins
	}
	if d.Auth != nil && d.Auth.ClientIP != nil {
		opt.Scheme = func(r *http.Request) string { return d.Auth.ClientIP.Scheme(r) }
	}
	return opt
}
