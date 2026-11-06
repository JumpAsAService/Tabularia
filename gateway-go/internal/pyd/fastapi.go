package pyd

import (
	"errors"
	"net/http"
	"strconv"
	"strings"
)

// Body is a request body as FastAPI hands it to the validation: absent (no bytes),
// parsed JSON, or the raw bytes of a content type that is not JSON.
type Body struct {
	present bool
	value   any
}

// Response is an answer FastAPI gives before (400, a JSON that does not parse) or
// instead of (422) running the endpoint.
type Response struct {
	Status      int
	ContentType string
	Body        []byte
}

// Write sends the response.
func (r *Response) Write(w http.ResponseWriter) {
	w.Header().Set("Content-Type", r.ContentType)
	w.Header().Set("Content-Length", strconv.Itoa(len(r.Body)))
	w.WriteHeader(r.Status)
	_, _ = w.Write(r.Body)
}

// ReadBody is FastAPI's body reading for a route with a body parameter
// (routing.py get_request_handler). It runs BEFORE the dependencies, so a JSON
// that does not parse is a 422 even for whoever is not signed in. With bytes:
//   - no Content-Type: the bytes stay bytes (strict_content_type);
//   - application/json or application/*+json: json.loads, its JSONDecodeError a
//     422 json_invalid at the error's position, any other failure a 400;
//   - any other type: the bytes stay bytes, and the model refuses them.
func ReadBody(raw []byte, contentType string) (Body, *Response) {
	if len(raw) == 0 {
		return Body{}, nil
	}
	if contentType == "" || !isJSONContentType(contentType) {
		return Body{present: true, value: RawBytes(raw)}, nil
	}
	v, err := loadsJSON(raw)
	if err != nil {
		var jde *JSONDecodeError
		if errors.As(err, &jde) {
			e := &Error{Type: "json_invalid", Loc: Loc{"body", jde.Pos}, Msg: "JSON decode error",
				Input: NewObject(), Ctx: []KV{{"error", jde.Msg}}}
			return Body{}, RenderErrors([]*Error{e})
		}
		return Body{}, BadRequestBody()
	}
	if v == nil {
		// JSON null is the same as no body: FastAPI tests `value is None`
		return Body{}, nil
	}
	return Body{present: true, value: v}, nil
}

// BadRequestBody is FastAPI's HTTPException(400) for a body it could not parse.
func BadRequestBody() *Response {
	return &Response{Status: http.StatusBadRequest, ContentType: "application/json",
		Body: []byte(`{"detail":"There was an error parsing the body"}`)}
}

// isJSONContentType is FastAPI's test through email.message.Message: the type
// before ';', trimmed and lower-cased, must have exactly one '/', main type
// "application" and subtype "json" or "*+json".
func isJSONContentType(ct string) bool {
	t, _, _ := strings.Cut(ct, ";")
	t = strings.ToLower(strings.TrimSpace(t))
	if strings.Count(t, "/") != 1 {
		return false
	}
	main, sub, _ := strings.Cut(t, "/")
	return main == "application" && (sub == "json" || strings.HasSuffix(sub, "+json"))
}

// Params validates an endpoint's parameters in FastAPI's order (solve_dependencies:
// path, query, then the body) and keeps every error.
type Params struct {
	errs []*Error
}

// Path validates a path parameter (always present: the router matched it).
func (p *Params) Path(name, value string, t *Type) any {
	v, errs := t.Validate(value)
	if errs != nil {
		p.errs = append(p.errs, prefix(errs, "path", name)...)
		return nil
	}
	return v
}

// Query validates a query parameter: the last value given wins, as Starlette's
// QueryParams.get; absent, it is the default, or `missing` when required.
func (p *Params) Query(q *Query, name string, t *Type, def ...any) any {
	raw, ok := q.Get(name)
	if !ok {
		if len(def) == 0 {
			p.errs = append(p.errs, &Error{Type: "missing", Loc: Loc{"query", name}, Msg: "Field required", Input: nil})
			return nil
		}
		return deepCopy(normNumber(def[0]))
	}
	v, errs := t.Validate(raw)
	if errs != nil {
		p.errs = append(p.errs, prefix(errs, "query", name)...)
		return nil
	}
	return v
}

// Body validates the body against its model. A required body that is absent (or
// JSON null) is `missing` at ["body"]; an optional one gives nil.
func (p *Params) Body(b Body, m *Model) *ModelValue { return p.body(b, m, false) }

// OptionalBody is a `body: Model | None = None` parameter.
func (p *Params) OptionalBody(b Body, m *Model) *ModelValue { return p.body(b, m, true) }

func (p *Params) body(b Body, m *Model, optional bool) *ModelValue {
	if !b.present {
		if !optional {
			p.errs = append(p.errs, &Error{Type: "missing", Loc: Loc{"body"}, Msg: "Field required", Input: nil})
		}
		return nil
	}
	v, errs := m.validate(b.value, vstate{})
	if errs != nil {
		p.errs = append(p.errs, prefix(errs, "body")...)
		return nil
	}
	return v.(*ModelValue)
}

// Errors returns the errors so far.
func (p *Params) Errors() []*Error { return p.errs }

// Failed reports whether any parameter was refused.
func (p *Params) Failed() bool { return len(p.errs) > 0 }

// Response is the 422 for the errors, or nil when there are none.
func (p *Params) Response() *Response {
	if len(p.errs) == 0 {
		return nil
	}
	return RenderErrors(p.errs)
}

// RenderErrors is the gateway's 422 (main.py `_invalid_request`): FastAPI's
// {"detail": [...]}; when a value cannot be written in JSON (NaN, infinity, a
// lone surrogate, bytes that are not UTF-8), every error loses "input" and "ctx";
// and if even that cannot be written, Starlette's bare 500.
func RenderErrors(errs []*Error) *Response {
	if b, err := dumpsJSON(detailObject(errs, true)); err == nil {
		return &Response{Status: http.StatusUnprocessableEntity, ContentType: "application/json", Body: b}
	}
	if b, err := dumpsJSON(detailObject(errs, false)); err == nil {
		return &Response{Status: http.StatusUnprocessableEntity, ContentType: "application/json", Body: b}
	}
	return &Response{Status: http.StatusInternalServerError, ContentType: "text/plain; charset=utf-8",
		Body: []byte("Internal Server Error")}
}

func detailObject(errs []*Error, full bool) *Object {
	list := make([]any, len(errs))
	for i, e := range errs {
		o := NewObject()
		o.Set("type", e.Type)
		o.Set("loc", []any(e.Loc))
		o.Set("msg", e.Msg)
		if full {
			o.Set("input", e.Input)
			if e.Ctx != nil {
				c := NewObject()
				for _, kv := range e.Ctx {
					c.Set(kv.K, kv.V)
				}
				o.Set("ctx", c)
			}
		}
		list[i] = o
	}
	d := NewObject()
	d.Set("detail", list)
	return d
}
