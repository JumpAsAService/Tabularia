package reports

import (
	"context"
	"errors"
	"fmt"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportparams"
)

// What a flow group's Pushmail cell checks of its reports and dashboards before it runs
// (services/pushmail.py report_errors, dashboard_errors): the values against the parameters,
// and who may read every dataset, without asking the engine anything.

// CheckValues is report_params.check_values: values_for without resolving the placeholders.
func CheckValues(def pyjson.Object, given Given) error {
	params := parameters(def)
	known := map[string]bool{}
	for _, p := range params {
		known[pystr.Str(get(p, "name"))] = true
	}
	for _, kv := range given {
		if !known[kv.Key] {
			return BadValue(kv.Key, "the report has no parameter by this name")
		}
	}
	all := make([][]any, len(params))
	for i, p := range params {
		g, has := given.Get(pystr.Str(get(p, "name")))
		gl, _ := g.([]any)
		ts, err := texts(p, gl, has)
		if err != nil {
			return err
		}
		all[i] = ts
	}
	_, err := converted(params, all, nil, false)
	return err
}

// ReadingProblem is reading_problem: why the person (`c`) could not read every dataset of the
// definition, "" when they can.
func ReadingProblem(ctx context.Context, q perm.Querier, c *perm.Checker, svc *aisvc.Client, def pyjson.Object) (string, error) {
	params := paramDefs(def)
	for _, d := range list(or(get(def, "datasets"), []any{})) {
		ds := obj(d)
		src := obj(get(ds, "source"))
		var err error
		switch get(src, "kind") {
		case "sql", "sql_script":
			var s *SQLSource
			if s, err = SQLOf(ctx, q, c, src); err == nil && s.Conn == nil {
				err = resolveCatalog(ctx, q, c, svc, s, params)
			}
		case "datasource":
			_, err = ReadableDatasource(ctx, q, c, asID(get(src, "datasource_id")))
		default:
			_, _, err = ReadableView(ctx, q, c, asID(get(src, "view_id")))
		}
		if err == nil {
			continue
		}
		var u *Unreadable
		var r *Refusal
		var pr *reportparams.Refusal
		switch {
		case errors.As(err, &u), errors.As(err, &r):
			return fmt.Sprintf("The dataset «%s»: %s", pystr.Str(get(ds, "name")), pystr.Within(Text(err))), nil
		case errors.As(err, &pr):
			return fmt.Sprintf("The dataset «%s»: %s", pystr.Str(get(ds, "name")), pystr.Within(pr.Message)), nil
		}
		return "", err
	}
	return "", nil
}
