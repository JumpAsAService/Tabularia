package api

import (
	"context"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// What the orchestration hands internal/flowresolver (services/flow_resolver.py), and the
// helpers that read a flow's definition.

// flowMode is the resolver's engine mode: in development the sources' samples, the panel's
// automatic one for the nodes without one (default_sample_rows).
func (d *Deps) flowMode(engineMode string) flowresolver.Mode {
	return flowresolver.Mode{Development: engineMode == "development",
		DefaultSampleRows: flowresolver.DefaultSampleRows(int64(d.Panel.PreviewSampleRows()))}
}

// flowDsResolver is the orchestration's resolve_ds: (d.bucket, d.key) if d and d.key else None.
func (d *Deps) flowDsResolver(ctx context.Context) flowresolver.DsResolver {
	return func(i any) (*flowresolver.Source, error) {
		n, ok := pyjson.IsInt(i)
		if !ok {
			return nil, nil
		}
		var b, k string
		if d.DB.QueryRow(ctx, `SELECT bucket, key FROM datasources WHERE id = $1`, n).Scan(&b, &k) != nil || k == "" {
			return nil, nil
		}
		return &flowresolver.Source{Bucket: b, Key: k}, nil
	}
}

func objectList(v any) []pyjson.Object {
	list, _ := v.([]any)
	out := make([]pyjson.Object, 0, len(list))
	for _, x := range list {
		out = append(out, pyjson.Obj(x))
	}
	return out
}

func nodeData(n pyjson.Object) pyjson.Object {
	return pyjson.Obj(pyjson.Or(n.Value("data"), pyjson.Object{}))
}

func textOrEmpty(v any) string {
	if !pyjson.Truthy(v) {
		return ""
	}
	return pyjson.Str(v)
}
