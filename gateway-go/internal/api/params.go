package api

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
)

// pathIDs reads the integer path parameters `names` (in the order the Python route
// declares them) and answers FastAPI's 422 when some is not one. A value past an
// int32 matches no row of the database: it reads as an id that does not exist (0).
func pathIDs(w http.ResponseWriter, r *http.Request, names ...string) ([]int64, bool) {
	var p pyd.Params
	vals := make([]any, len(names))
	for i, name := range names {
		vals[i] = p.Path(name, r.PathValue(name), pyd.Int())
	}
	if resp := p.Response(); resp != nil {
		resp.Write(w)
		return nil, false
	}
	return int32IDs(vals), true
}

// int32IDs turns validated ints into ids: one that no int4 column can hold is 0.
func int32IDs(vals []any) []int64 {
	out := make([]int64, len(vals))
	for i, v := range vals {
		if n, ok := v.(int64); ok && n <= 1<<31-1 && n >= -(1<<31) {
			out[i] = n
		}
	}
	return out
}
