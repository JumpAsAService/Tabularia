package api

import (
	"fmt"
	"net/http"
	"sync"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportdef"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// problemRefusal is Problem.http: the 422 of a definition with that problem.
func problemRefusal(err error, definition any) error {
	p, ok := reportdef.AsProblem(err)
	if !ok {
		return err
	}
	return &reports.Refusal{Status: http.StatusUnprocessableEntity, Detail: reportdef.Refusal([]pyjson.Object{p.Dict(definition)}, &p.Where)}
}

// parameters is GET /reports/{report_id}/parameters (report_parameters): the parameters as the
// form shows them, the values of the query string steering the choices.
func (h *objectsAPI) parameters(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, h.param)
	if !ok {
		return
	}
	ids = inColumn(ids)
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, ids[0])
	if it == nil {
		return
	}
	content, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	def, ok := content.(pyjson.Object)
	if !ok {
		h.fail(w, r, versioned.ErrNotAnObject)
		return
	}
	out, err := h.resolver(c).ParametersOut(ctx, def, queryParams(pyd.ParseQuery(r.URL.RawQuery)),
		func(string) (pyjson.Object, error) { return def, nil }, "")
	if err != nil {
		h.writeErr(w, r, nil, err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// parametersEdited is POST /reports/{report_id}/parameters (report_parameters_being_edited): the
// same for the editor's definition, not saved yet (EDIT on the folder).
func (h *objectsAPI) parametersEdited(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	rid := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	b := params.Body(body, gatewaymodels.ParametersRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	if _, ok := h.beingEdited(w, r, c, rid, nil); !ok {
		return
	}
	definition := toPython(b.Get("definition"))
	def, err := reportdef.CheckParameters(definition)
	if err != nil {
		h.writeErr(w, r, raw, problemRefusal(err, definition))
		return
	}
	given, err := reports.GivenParams(toPython(b.Get("params")).(pyjson.Object))
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	out, err := h.resolver(c).ParametersOut(ctx, def, given, func(name string) (pyjson.Object, error) {
		return reportdef.CheckDataset(definition, name)
	}, "")
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// fieldsOut is FieldsOut, without what is None (response_model_exclude_none).
type fieldsOut struct {
	Dataset string          `json:"dataset"`
	Fields  []reports.Field `json:"fields"`
	Rows    *json64         `json:"rows,omitempty"`
	Over    *bool           `json:"over,omitempty"`
	MaxRows *json64         `json:"max_rows,omitempty"`
}

type json64 = int64

// fields is POST /reports/{report_id}/fields (report_fields): a dataset's columns, of the saved
// definition (VIEW) or of the editor's (EDIT), and its rows counted at the same time.
func (h *objectsAPI) fields(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	rid := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	b := params.Body(body, gatewaymodels.ReportsFieldsRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, idOf(rid))
	if it == nil {
		return
	}
	dataset := b.Get("dataset").(string)
	var def pyjson.Object
	if dv := b.Get("definition"); dv != nil {
		if !h.ensure(w, r, c, it.ProjectID, perm.Edit) {
			return
		}
		definition := toPython(dv)
		checked, err := reportdef.CheckDataset(definition, dataset)
		if err != nil {
			h.writeErr(w, r, raw, problemRefusal(err, definition))
			return
		}
		def = checked
	} else {
		content, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
		if err != nil {
			h.fail(w, r, err)
			return
		}
		if def, _ = content.(pyjson.Object); def == nil {
			h.fail(w, r, versioned.ErrNotAnObject)
			return
		}
	}
	h.fieldsOf(w, r, c, raw, def, dataset, fmt.Sprintf("u%d:%s:%d", p.User.ID, h.kind.Label, it.ID))
}

// fieldsOf reads the columns and, at the same time, counts the rows (never an error of its own).
func (h *objectsAPI) fieldsOf(w http.ResponseWriter, r *http.Request, c *perm.Checker, raw []byte, def pyjson.Object, dataset, slot string) {
	ctx := r.Context()
	rs := h.resolver(c)
	toCount, err := rs.ToCount(ctx, def, dataset)
	if err != nil {
		h.writeErr(w, r, raw, err)
		return
	}
	var counted pyjson.Object
	var wg sync.WaitGroup
	if toCount != nil {
		wg.Add(1)
		go func() {
			defer wg.Done()
			counted = rs.CountRows(ctx, toCount, slot+":count")
		}()
	}
	found, err := rs.Fields(ctx, def, dataset, slot+":fields")
	wg.Wait()
	if err != nil {
		if h.inOurWords != nil {
			err = h.inOurWords(err)
		}
		h.writeErr(w, r, raw, err)
		return
	}
	out := fieldsOut{Dataset: dataset, Fields: found}
	if counted != nil {
		rows, _ := counted.Get("rows")
		over, _ := counted.Get("over")
		maxRows, _ := counted.Get("max_rows")
		rv, ov, mv := rows.(int64), over.(bool), maxRows.(int64)
		out.Rows, out.Over, out.MaxRows = &rv, &ov, &mv
	}
	server.JSON(w, http.StatusOK, out)
}
