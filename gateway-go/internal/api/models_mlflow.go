package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// The lists a Predict node chooses its model from (docs/specs/statistical-models.md,
// «Predict» → «Choosing the model, in the form»), read from the MLflow server by the gateway
// with the connection's credentials; the person needs CONNECT on the connection's folder.
//
//	GET /models/mlflow/{connection_id}/experiments
//	→ {"experiments": [{"id": "3", "name": "Pasta/price elasticity"}, …]}        (by name)
//	GET /models/mlflow/{connection_id}/versions?experiment=<name>
//	→ {"versions": [{"model": "price-elasticity", "version": "7", "kind": "linear",
//	                 "created_at": "2026-11-19T10:00:00Z", "flow": "Pricing", "node": "Elasticity",
//	                 "metric": {"name": "adj_r_squared", "value": 0.83}}, …]}  (newest first)
//
// Only the versions Tabularia registered (the version's tag tabularia.model) whose run is in the
// experiment («Provisional decisions», 5): the experiment's runs that carry the tag, then the
// versions registered from them. `kind` is the tag (the catalogue's model), `flow` and `node`
// the run's tags, `metric` the model's main number among the run's metrics.
//
// A failure of MLflow is 502 with the tracking client's sentence (never the server's text); an
// experiment MLflow does not have is 404.

func init() {
	register("models_mlflow", func(d *Deps) []server.Route {
		route := func(pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: http.MethodGet, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(d, w, r, p) })}
		}
		return []server.Route{
			route("/models/mlflow/{connection_id}/experiments", mlflowExperiments),
			route("/models/mlflow/{connection_id}/versions", mlflowVersions),
		}
	})
}

// mlflowConn is an `mlflow` connection the person may use, with its secret decrypted.
type mlflowConn struct {
	id   int64
	name string
	cfg  mlflowconn.Config
	ui   string // the base of the links: the connection's UI address, else its tracking URI
	host string // the tracking URI as saved
}

// mlflowConnRefused is why a person may not use an MLflow connection: notFound when it is not an
// MLflow connection (any more).
type mlflowConnRefused struct {
	msg      string
	notFound bool
}

func (e *mlflowConnRefused) Error() string { return e.msg }

// mlflowConnection loads the connection #id for the person: an *mlflowConnRefused when it is not
// an MLflow connection or the person lacks CONNECT on its folder.
func (d *Deps) mlflowConnection(ctx context.Context, user *auth.User, id int64) (*mlflowConn, error) {
	conn, err := connpayload.Load(ctx, d.DB, id)
	if err != nil {
		return nil, err
	}
	if conn == nil || conn.DBType != "mlflow" {
		return nil, &mlflowConnRefused{msg: fmt.Sprintf("the MLflow connection #%d does not exist any more: choose another one", id), notFound: true}
	}
	if ok, err := perm.For(d.DB, user).Has(ctx, conn.ProjectID, perm.Connect); err != nil {
		return nil, err
	} else if !ok {
		return nil, &mlflowConnRefused{msg: fmt.Sprintf("%s may not use the MLflow connection «%s»: it needs CONNECT on its folder", user.Email, conn.Name)}
	}
	secretText := ""
	if conn.PasswordEncrypted != "" {
		if d.Fernet == nil {
			return nil, errEncrypt
		}
		if secretText, err = d.Fernet.Decrypt(conn.PasswordEncrypted); err != nil {
			return nil, err
		}
	}
	cfg := mlflowConfig(conn.Host, conn.Username, secretText, conn.Extra)
	cfg.Timeout = mlflowCallTimeout
	ui := mlflowconn.ParseOptions(conn.Extra).UIURL
	if ui == "" {
		ui = cfg.URI
	}
	return &mlflowConn{id: conn.ID, name: conn.Name, cfg: cfg, ui: strings.TrimRight(ui, "/"), host: conn.Host}, nil
}

// mlflowBrowse is the part both routes share: the connection, or the answer already given.
func mlflowBrowse(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) (*mlflowConn, context.Context, context.CancelFunc, bool) {
	ids, ok := pathInts(w, r, "connection_id")
	if !ok {
		return nil, nil, nil, false
	}
	q := d.req(w, r, p)
	conn, err := d.mlflowConnection(q.ctx, &p.User, ids[0])
	var refused *mlflowConnRefused
	switch {
	case errors.As(err, &refused) && refused.notFound:
		q.refuse(http.StatusNotFound, "Connection not found")
		return nil, nil, nil, false
	case errors.As(err, &refused):
		q.refuse(http.StatusForbidden, upperFirst(refused.msg))
		return nil, nil, nil, false
	case err != nil:
		q.fail(err)
		return nil, nil, nil, false
	}
	ctx, cancel := context.WithTimeout(q.ctx, 2*time.Minute)
	return conn, ctx, cancel, true
}

// mlflowAnswerError answers a failure of MLflow: 404 for what it does not have, 502 otherwise.
func mlflowAnswerError(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, err error) {
	q := d.req(w, r, p)
	var nf *mlflowconn.NotFound
	var me *mlflowconn.Error
	switch {
	case errors.As(err, &nf):
		q.refuse(http.StatusNotFound, upperFirst(nf.Error()))
	case errors.As(err, &me):
		q.refuse(http.StatusBadGateway, "MLflow: "+me.Error())
	case r.Context().Err() == nil && errors.Is(err, context.DeadlineExceeded):
		q.refuse(http.StatusGatewayTimeout, "MLflow: the tracking server did not answer in time")
	default:
		q.fail(err)
	}
}

// mlflowExperiments is GET /models/mlflow/{connection_id}/experiments.
func mlflowExperiments(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	conn, ctx, cancel, ok := mlflowBrowse(d, w, r, p)
	if !ok {
		return
	}
	defer cancel()
	list, err := mlflowconn.NewTracker(conn.cfg).Experiments(ctx)
	if err != nil {
		mlflowAnswerError(d, w, r, p, err)
		return
	}
	out := make([]any, 0, len(list))
	for _, e := range list {
		out = append(out, pyjson.Object{{Key: "id", Value: e.ID}, {Key: "name", Value: e.Name}})
	}
	server.JSON(w, http.StatusOK, pyjson.Object{{Key: "experiments", Value: out}})
}

// mlflowVersionsMax is the most versions the list gives (the newest).
const mlflowVersionsMax = 500

// mlflowVersions is GET /models/mlflow/{connection_id}/versions?experiment=<name>.
func mlflowVersions(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	experiment := strings.TrimSpace(r.URL.Query().Get("experiment"))
	if experiment == "" {
		d.req(w, r, p).refuse(http.StatusUnprocessableEntity, "Name the experiment")
		return
	}
	conn, ctx, cancel, ok := mlflowBrowse(d, w, r, p)
	if !ok {
		return
	}
	defer cancel()
	list, err := tabulariaVersions(ctx, mlflowconn.NewTracker(conn.cfg), experiment)
	if err != nil {
		mlflowAnswerError(d, w, r, p, err)
		return
	}
	out := make([]any, 0, len(list))
	for _, v := range list {
		o := pyjson.Object{{Key: "model", Value: v.version.Name}, {Key: "version", Value: v.version.Version},
			{Key: "kind", Value: v.version.Tags["tabularia.model"]}, {Key: "created_at", Value: nil},
			{Key: "flow", Value: nilIfEmpty(v.run.Tags["tabularia.flow_name"])}, {Key: "node", Value: nilIfEmpty(v.run.Tags["tabularia.node_name"])},
			{Key: "metric", Value: nil}}
		if v.version.Created > 0 {
			o = o.Set("created_at", time.UnixMilli(v.version.Created).UTC().Format(time.RFC3339))
		}
		if name, value, found := mainMetric(v.version.Tags["tabularia.model"], v.run.Metrics); found {
			o = o.Set("metric", pyjson.Object{{Key: "name", Value: name}, {Key: "value", Value: value}})
		}
		out = append(out, o)
	}
	server.JSON(w, http.StatusOK, pyjson.Object{{Key: "versions", Value: out}})
}

// tabularia's version and the run it was registered from.
type tabulariaVersion struct {
	version mlflowconn.ModelVersion
	run     mlflowconn.RunInfo
}

// tabulariaVersions are the versions Tabularia registered from the experiment's runs, newest
// first (then by model and number), at most mlflowVersionsMax.
func tabulariaVersions(ctx context.Context, tr *mlflowconn.Tracker, experiment string) ([]tabulariaVersion, error) {
	expID, err := tr.ExperimentID(ctx, experiment)
	if err != nil {
		return nil, err
	}
	runs, err := tr.TabulariaRuns(ctx, expID)
	if err != nil {
		return nil, err
	}
	byID := map[string]mlflowconn.RunInfo{}
	ids := make([]string, 0, len(runs))
	for _, r := range runs {
		byID[r.ID] = r
		ids = append(ids, r.ID)
	}
	versions, err := tr.VersionsOfRuns(ctx, ids)
	if err != nil {
		return nil, err
	}
	var out []tabulariaVersion
	for _, v := range versions {
		run, inExperiment := byID[v.RunID]
		if !inExperiment || v.Tags["tabularia.model"] == "" {
			continue
		}
		out = append(out, tabulariaVersion{version: v, run: run})
	}
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i].version, out[j].version
		if a.Created != b.Created {
			return a.Created > b.Created
		}
		if a.Name != b.Name {
			return a.Name < b.Name
		}
		return a.Number() > b.Number()
	})
	if len(out) > mlflowVersionsMax {
		out = out[:mlflowVersionsMax]
	}
	return out, nil
}

// mainMetrics is the number the list shows of each model, in order of preference.
var mainMetrics = map[string][]string{
	"linear":       {"adj_r_squared", "r_squared"},
	"logistic":     {"auc"},
	"cox":          {"concordance"},
	"kaplan_meier": {"events", "n"},
	"arima":        {"aicc", "aic"},
}

func mainMetric(model string, metrics map[string]float64) (string, float64, bool) {
	for _, name := range mainMetrics[model] {
		if v, ok := metrics[name]; ok {
			return name, v, true
		}
	}
	return "", 0, false
}
