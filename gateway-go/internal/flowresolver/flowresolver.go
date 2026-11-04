// Package flowresolver is services/flow_resolver.py: a flow's definition resolved into the
// bodies of its runs on the server, the twin of the editor's resolveChain/executeOutputRuns.
// The orchestrator needs it (a scheduled flow runs without a browser), and so does the dbt
// export (the chains of the Outputs, with the node of every step).
//
// For every Output node it makes a run's body (bucket, input_key, operations,
// publish|destination|email), walking up the chain and injecting the right branches of
// join/union and the driver and body of the foreach. A datasource source is resolved again to
// its CURRENT snapshot through the caller's DsResolver: a refresh deletes the old blob, so a
// key frozen in the definition would give a 404.
//
// The definition is JSON nobody checked (pyjson values): where the Python code raises on it
// (an AttributeError on a node that is not a dict, a KeyError on a node without an id) these
// functions return a *pyval.Error with the same type and message, which callers that catch
// those exceptions (the dbt export) say as Python says them.
package flowresolver

import (
	"math"
	"sort"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyval"
)

// Source is a resolved source, Python's (bucket, key) tuple: the bucket may be None (an
// upload's node without one), the key is what the node or the datasource holds.
type Source struct {
	Bucket any
	Key    any
}

// DsResolver is `resolve_ds`: the current snapshot of a datasource by the id the node holds
// (any JSON value: the caller converts it, int(ds_id)), nil when it has none. An error is
// raised as it is (a database error).
type DsResolver func(dsID any) (*Source, error)

// StepNodeTypes is STEP_NODE_TYPES: the nodes that run outside the engine and hand their
// output to the nodes after them.
var StepNodeTypes = []string{"script", "container"}

// Error is FlowResolveError: the definition cannot run. Code and Params say it in the
// reader's language: "no_outputs", "output_no_source" {output}, "flow_loop" {node},
// "script_no_source" {script}.
type Error struct {
	Message string
	Code    string
	Params  pyjson.Object
}

func (e *Error) Error() string { return e.Message }

func raise(e *Error) { pyval.Throw(e) }

// ── upstream filters on the sources ─────────────────────────────────────────

var sourceFilterOperators = map[string]bool{"eq": true, "ne": true, "gt": true, "ge": true, "lt": true, "le": true,
	"in": true, "not_in": true, "between": true, "contains": true, "starts_with": true, "ends_with": true,
	"is_null": true, "is_not_null": true}

// SourceFilterOperations is source_filter_operations: the IR `filter` operations of a source
// node's conditions (`data.filters`), the incomplete ones ignored as the editor does.
func SourceFilterOperations(data any) []any {
	filters := pyval.Get(pyval.Or(data, pyjson.Object{}), "filters")
	list, ok := filters.([]any)
	if !ok {
		return []any{}
	}
	ops := []any{}
	for _, c := range list {
		cond, ok := c.(pyjson.Object)
		if !ok {
			continue
		}
		column, operator := cond.Value("column"), cond.Value("operator")
		col, isStr := column.(string)
		if !isStr || col == "" || !inOperators(operator) {
			continue
		}
		params := pyjson.Object{{Key: "column", Value: column}, {Key: "operator", Value: operator}}
		if op, _ := operator.(string); op != "is_null" && op != "is_not_null" {
			value := cond.Value("value")
			if value == nil {
				continue
			}
			l, isList := value.([]any)
			if (op == "in" || op == "not_in") && (!isList || len(l) == 0) {
				continue
			}
			if op == "between" && (!isList || len(l) != 2) {
				continue
			}
			params = append(params, pyjson.KV{Key: "value", Value: value})
		}
		ops = append(ops, pyjson.Object{{Key: "type", Value: "filter"}, {Key: "params", Value: params}})
	}
	return ops
}

// inOperators is `operator in SOURCE_FILTER_OPERATORS` (a frozenset: an unhashable operator
// raises).
func inOperators(op any) bool {
	pyval.Key(op)
	s, ok := op.(string)
	return ok && sourceFilterOperators[s]
}

// ── the development sample on the sources ───────────────────────────────────

// DevSampleMark is DEV_SAMPLE_MARK; MaxSampleRows is MAX_SAMPLE_ROWS.
const (
	DevSampleMark = "_dev_sample"
	MaxSampleRows = 100_000_000
)

// DevSampleOperation is dev_sample_operation: the IR operation of a source node's sample,
// nil when not sampled. defaultRows > 0 is the automatic sample for the nodes without one.
func DevSampleOperation(data any, defaultRows int64) pyjson.Object {
	sample := pyval.Get(pyval.Or(data, pyjson.Object{}), "sample")
	s, ok := sample.(pyjson.Object)
	if !ok {
		if defaultRows > 0 {
			return limitOp(min(defaultRows, MaxSampleRows))
		}
		return nil
	}
	switch mode := s.Value("mode"); {
	case pyval.Eq(mode, "off"):
		return nil
	case pyval.Eq(mode, "first"):
		var n int64
		if err := pyval.Catch(func() { n = pyval.Int(pyval.Or(s.Value("rows"), int64(0))) }); err != nil {
			if pe, ok := err.(*pyval.Error); ok && (pe.Type == "TypeError" || pe.Type == "ValueError") {
				return nil
			}
			pyval.Throw(err)
		}
		if n <= 0 {
			return nil
		}
		return limitOp(min(n, MaxSampleRows))
	case pyval.Eq(mode, "random"):
		var pct float64
		if err := pyval.Catch(func() { pct = pyval.Float(pyval.Or(s.Value("percent"), int64(0))) }); err != nil {
			if pe, ok := err.(*pyval.Error); ok && (pe.Type == "TypeError" || pe.Type == "ValueError") {
				return nil
			}
			pyval.Throw(err)
		}
		if !(0 < pct && pct < 100) {
			return nil
		}
		return pyjson.Object{{Key: "type", Value: "sample"}, {Key: "params", Value: pyjson.Object{
			{Key: "fraction", Value: pct / 100}, {Key: "seed", Value: int64(42)}, {Key: DevSampleMark, Value: true}}}}
	}
	return nil
}

func limitOp(n int64) pyjson.Object {
	return pyjson.Object{{Key: "type", Value: "limit"}, {Key: "params", Value: pyjson.Object{
		{Key: "n", Value: n}, {Key: DevSampleMark, Value: true}}}}
}

// StripDevSampleOps is strip_dev_sample_ops: the operations marked `_dev_sample` removed
// everywhere (the nested right/driver/body too), and how many were.
func StripDevSampleOps(operations []any) ([]any, int) {
	removed := 0
	var cleanOps func(ops any) []any
	var cleanParams func(params any) any
	cleanOps = func(ops any) []any {
		out := []any{}
		if !pyval.Truthy(ops) {
			return out
		}
		for _, op := range pyval.Iter(ops) {
			o, ok := op.(pyjson.Object)
			if !ok {
				out = append(out, op)
				continue
			}
			params := pyval.Or(o.Value("params"), pyjson.Object{})
			if p, ok := params.(pyjson.Object); ok && pyval.Truthy(p.Value(DevSampleMark)) {
				removed++
				continue
			}
			out = append(out, append(pyjson.Object{}, o...).Set("params", cleanParams(params)))
		}
		return out
	}
	cleanParams = func(params any) any {
		p, ok := params.(pyjson.Object)
		if !ok {
			return params
		}
		n := append(pyjson.Object{}, p...)
		for _, key := range []string{"right", "driver"} {
			if ref, ok := n.Value(key).(pyjson.Object); ok {
				if _, has := ref.Get("operations"); has {
					n = n.Set(key, append(pyjson.Object{}, ref...).Set("operations", cleanOps(ref.Value("operations"))))
				}
			}
		}
		if body, ok := n.Value("body").([]any); ok {
			n = n.Set("body", cleanOps(body))
		}
		return n
	}
	out := cleanOps(operations)
	return out, removed
}

// ── the edges ───────────────────────────────────────────────────────────────

// SeqTargetHandle and SeqSourceHandle are the handles of the sequence edges (the
// orchestration), not part of the data.
const (
	SeqTargetHandle = "seq-in"
	SeqSourceHandle = "seq-out"
)

func isSeqEdge(e any) bool {
	return pyval.Eq(pyval.Get(e, "targetHandle"), SeqTargetHandle) || pyval.Eq(pyval.Get(e, "sourceHandle"), SeqSourceHandle)
}

// inputs is a target's {left, right} by the data edges' targetHandle.
type inputs struct{ left, right any }

func incoming(edges []any) *pyval.Dict {
	m := pyval.NewDict()
	for _, e := range edges {
		if isSeqEdge(e) {
			continue
		}
		handle := pyval.Or(pyval.Get(e, "targetHandle"), "left")
		entry, _ := m.SetDefault(pyval.Get(e, "target"), &inputs{}).(*inputs)
		if pyval.Eq(handle, "right") {
			entry.right = pyval.Get(e, "source")
		} else {
			entry.left = pyval.Get(e, "source")
		}
	}
	return m
}

func inputOf(inc *pyval.Dict, id any) *inputs {
	if x, ok := inc.Lookup(id); ok {
		return x.(*inputs)
	}
	return &inputs{}
}

// resolveSource is _resolve_source: a catalog datasource → its CURRENT snapshot (resolved
// again by id); an upload → the file's key.
func resolveSource(node any, resolveDS DsResolver) *Source {
	data := pyval.Or(pyval.Get(node, "data"), pyjson.Object{})
	if dsID := pyval.Get(data, "datasourceId"); dsID != nil {
		resolved, err := resolveDS(dsID)
		pyval.Check(err)
		if resolved != nil && pyval.Truthy(resolved.Key) {
			return resolved
		}
		return nil
	}
	if key := pyval.Get(data, "parquetKey"); pyval.Truthy(key) {
		return &Source{Bucket: pyval.Get(data, "bucket"), Key: key}
	}
	return nil
}

// Mode is the engine mode: only "development" injects the sources' samples.
type Mode struct {
	Development bool
	// DefaultSampleRows is PREVIEW__DEFAULT_SAMPLE_ROWS (the panel's value), read only in
	// development: the automatic sample of the nodes without one
	DefaultSampleRows int64
}

// ScriptOutputs is what a Script or Container node wrote in this orchestration, by node id:
// the nodes after it start from there.
type ScriptOutputs map[string]*Source // pyval.Key(node id) → source

func (s ScriptOutputs) get(id any) *Source {
	if s == nil {
		return nil
	}
	return s[pyval.Key(id)]
}

// ScriptOutputKey is the key of a node id in ScriptOutputs.
func ScriptOutputKey(id any) string { return pyval.Key(id) }

type resolver struct {
	byID          *pyval.Dict
	scriptOutputs ScriptOutputs
	edges         []any
	inc           *pyval.Dict
	resolveDS     DsResolver
	devSampling   bool
	defaultRows   int64
	inProgress    map[string]bool
}

func newResolver(nodes, edges any, resolveDS DsResolver, mode Mode, scriptOutputs ScriptOutputs) *resolver {
	byID := pyval.NewDict()
	for _, n := range pyval.Iter(nodes) {
		byID.Set(pyval.Index(n, "id"), n)
	}
	e := pyval.Iter(edges)
	r := &resolver{byID: byID, scriptOutputs: scriptOutputs, edges: e, inc: incoming(e), resolveDS: resolveDS,
		devSampling: mode.Development, inProgress: map[string]bool{}}
	if r.devSampling {
		r.defaultRows = min(mode.DefaultSampleRows, MaxSampleRows)
	}
	return r
}

// once is _once: meeting the same call again inside itself means the edges close a loop.
func (r *resolver) once(call pyval.Tuple, nodeID any, f func()) {
	k := pyval.Key(call)
	if r.inProgress[k] {
		node := pyval.Or(r.byID.Get(nodeID), pyjson.Object{})
		label := pyval.Str(pyval.Or(pyval.Get(pyval.Or(pyval.Get(node, "data"), pyjson.Object{}), "label"), nodeID))
		raise(&Error{Message: "the flow has a loop through the node «" + label + "»: remove the edge that closes it",
			Code: "flow_loop", Params: pyjson.Object{{Key: "node", Value: label}}})
	}
	r.inProgress[k] = true
	defer delete(r.inProgress, k)
	f()
}

func (r *resolver) chain(targetID any) (*Source, []any) {
	src, ops, _ := r.chainWithIDs(targetID)
	return src, ops
}

func (r *resolver) chainWithIDs(targetID any) (src *Source, ops []any, ids []any) {
	r.once(pyval.Tuple{"chain", targetID}, targetID, func() { src, ops, ids = r.doChain(targetID) })
	return
}

func (r *resolver) doChain(targetID any) (*Source, []any, []any) {
	target := r.byID.Get(targetID)
	if pyval.Truthy(target) && pyval.Truthy(pyval.Get(target, "parentNode")) {
		parent := pyval.Index(target, "parentNode")
		upstreamID := inputOf(r.inc, parent).left
		var upSrc *Source
		upOps, upIDs := []any{}, []any{}
		if pyval.Truthy(upstreamID) {
			upSrc, upOps, upIDs = r.chainWithIDs(upstreamID)
		}
		op := r.operationFor(parent, nil)
		return upSrc, append(append([]any{}, upOps...), op), append(append([]any{}, upIDs...), parent)
	}
	var opIDs []any
	seen := map[string]bool{}
	var source *Source
	var sourceID any
	headOps := []any{}
	cur := targetID
	for pyval.Truthy(cur) && !seen[pyval.Key(cur)] {
		seen[pyval.Key(cur)] = true
		node := r.byID.Get(cur)
		if !pyval.Truthy(node) {
			break
		}
		typ := pyval.Get(node, "type")
		if inStrings(typ, StepNodeTypes...) {
			source = r.scriptOutputs.get(cur)
			sourceID = cur
			break
		}
		if pyval.Eq(typ, "source") {
			source = resolveSource(node, r.resolveDS)
			sourceID = cur
			data := pyval.Or(pyval.Get(node, "data"), pyjson.Object{})
			headOps = SourceFilterOperations(data)
			if r.devSampling {
				if op := DevSampleOperation(data, r.defaultRows); op != nil {
					headOps = append(headOps, op)
				}
			}
			break
		}
		if inStrings(typ, "operation", "foreach") {
			opIDs = append(opIDs, cur)
		}
		cur = inputOf(r.inc, cur).left
	}
	for i, j := 0, len(opIDs)-1; i < j; i, j = i+1, j-1 {
		opIDs[i], opIDs[j] = opIDs[j], opIDs[i]
	}
	ops := append([]any{}, headOps...)
	ids := make([]any, 0, len(headOps)+len(opIDs))
	for range headOps {
		ids = append(ids, pyval.Str(sourceID)+"#head")
	}
	for _, id := range opIDs {
		ops = append(ops, r.operationFor(id, nil))
	}
	ids = append(ids, opIDs...)
	return source, ops, ids
}

func inStrings(v any, options ...string) bool {
	for _, o := range options {
		if pyval.Eq(v, o) {
			return true
		}
	}
	return false
}

func (r *resolver) operationFor(nodeID, until any) (op pyjson.Object) {
	r.once(pyval.Tuple{"operation", nodeID, until}, nodeID, func() { op = r.buildOperation(nodeID, until) })
	return
}

func (r *resolver) buildOperation(nodeID, until any) pyjson.Object {
	node, ok := r.byID.Lookup(nodeID)
	if !ok {
		pyval.Raise("KeyError", "%s", pyjson.Repr(nodeID))
	}
	data := pyval.Or(pyval.Get(node, "data"), pyjson.Object{})
	params := pyval.DictOf(pyval.Or(pyval.Get(data, "params"), pyjson.Object{}))
	opType := pyval.Get(data, "opType")
	if inStrings(opType, "join", "union") {
		if rightID := inputOf(r.inc, nodeID).right; pyval.Truthy(rightID) {
			if src, ops := r.chain(rightID); src != nil {
				params = params.Set("right", pyjson.Object{{Key: "source", Value: pyjson.Object{{Key: "bucket", Value: src.Bucket},
					{Key: "key", Value: src.Key}}}, {Key: "operations", Value: ops}})
			}
		}
	}
	if pyval.Eq(pyval.Get(node, "type"), "foreach") {
		if driverID := inputOf(r.inc, nodeID).right; pyval.Truthy(driverID) {
			if src, ops := r.chain(driverID); src != nil {
				params = params.Set("driver", pyjson.Object{{Key: "source", Value: pyjson.Object{{Key: "bucket", Value: src.Bucket},
					{Key: "key", Value: src.Key}}}, {Key: "operations", Value: ops}})
			}
		}
		params = params.Set("body", r.containerBody(nodeID, until))
	}
	return pyjson.Object{{Key: "type", Value: opType}, {Key: "params", Value: params}}
}

func (r *resolver) containerBody(containerID, until any) []any {
	var children []any
	for _, n := range r.byID.Values() {
		if pyval.Eq(pyval.Get(n, "parentNode"), containerID) {
			children = append(children, n)
		}
	}
	if len(children) == 0 {
		return []any{}
	}
	childIDs := pyval.NewDict()
	for _, c := range children {
		childIDs.Set(pyval.Index(c, "id"), true)
	}
	next := pyval.NewDict()
	for _, e := range r.edges {
		if childIDs.Has(pyval.Get(e, "source")) && childIDs.Has(pyval.Get(e, "target")) &&
			pyval.Eq(pyval.Or(pyval.Get(e, "targetHandle"), "left"), "left") {
			next.Set(pyval.Index(e, "source"), pyval.Index(e, "target"))
		}
	}
	var cur any
	for _, c := range children {
		id := pyval.Index(c, "id")
		if !childIDs.Has(inputOf(r.inc, id).left) {
			cur = id
			break
		}
	}
	ops := []any{}
	seen := map[string]bool{}
	for pyval.Truthy(cur) && !seen[pyval.Key(cur)] {
		seen[pyval.Key(cur)] = true
		ops = append(ops, r.operationFor(cur, nil))
		if pyval.Eq(cur, until) {
			break
		}
		cur = next.Get(cur)
	}
	return ops
}

// ── the Outputs ─────────────────────────────────────────────────────────────

func outputLabel(node any) string {
	d := pyval.Or(pyval.Get(node, "data"), pyjson.Object{})
	switch dt := pyval.Or(pyval.Get(d, "destType"), "datasource"); {
	case pyval.Eq(dt, "database"):
		return "table " + pyval.Str(pyval.Or(pyval.Get(d, "table"), "…"))
	case pyval.Eq(dt, "s3"):
		return "S3 " + pyval.Str(pyval.Or(pyval.Get(d, "s3Key"), "…"))
	case pyval.Eq(dt, "sftp"):
		return "SFTP " + pyval.Str(pyval.Or(pyval.Get(d, "sftpFileName"), "…"))
	}
	return "datasource “" + pyval.Str(pyval.Or(pyval.Get(d, "name"), "…")) + "”"
}

// outputName is _output_name: what the user calls an Output.
func outputName(node any) string {
	d := pyval.Or(pyval.Get(node, "data"), pyjson.Object{})
	dt := pyval.Or(pyval.Get(d, "destType"), "datasource")
	var name any
	switch {
	case pyval.Eq(dt, "database"):
		name = pyval.Get(d, "table")
	case pyval.Eq(dt, "s3"):
		name = pyval.Get(d, "s3Key")
	case pyval.Eq(dt, "sftp"):
		name = pyval.Get(d, "sftpFileName")
	default:
		name = pyval.Get(d, "name")
	}
	if s := pyval.Strip(pyval.Str(pyval.Or(name, ""))); s != "" {
		return s
	}
	return pyval.Str(pyval.Or(pyval.Get(node, "id"), ""))
}

func noSource(node any) *Error {
	return &Error{Message: "output " + outputLabel(node) + ": no source with data upstream (or the source datasource has no snapshot)",
		Code: "output_no_source", Params: pyjson.Object{{Key: "output", Value: outputName(node)}}}
}

// outputBody is _output_body: the run body (RunCreate's shape) of ONE Output node.
func outputBody(node any, src *Source, operations []any, defaultBucket string) pyjson.Object {
	body := pyjson.Object{{Key: "bucket", Value: pyval.Or(src.Bucket, defaultBucket)}, {Key: "input_key", Value: src.Key},
		{Key: "operations", Value: operations}}
	d := pyval.Or(pyval.Get(node, "data"), pyjson.Object{})
	get := func(k string) any { return pyval.Get(d, k) }
	orEmpty := func(k string) any { return pyval.Or(get(k), "") }
	switch destType := pyval.Or(get("destType"), "datasource"); {
	case pyval.Eq(destType, "email"):
		body = append(body, pyjson.KV{Key: "email", Value: pyjson.Object{
			{Key: "connection_id", Value: get("connectionId")},
			{Key: "to", Value: pyval.Or(get("emailTo"), []any{})},
			{Key: "cc", Value: pyval.Or(get("emailCc"), []any{})},
			{Key: "subject", Value: orEmpty("emailSubject")},
			{Key: "body", Value: orEmpty("emailBody")},
			{Key: "body_is_html", Value: pyval.Truthy(get("emailHtml"))},
			{Key: "attachment_name", Value: pyval.Strip(orEmpty("attachmentName"))},
			{Key: "attachment_format", Value: pyval.Or(get("attachmentFormat"), "xlsx")},
			{Key: "stop_on_failure", Value: get("stopOnFailure") != false},
		}})
	case pyval.Eq(destType, "database"):
		body = append(body, pyjson.KV{Key: "destination", Value: pyjson.Object{
			{Key: "type", Value: "database"},
			{Key: "connection_id", Value: get("connectionId")},
			{Key: "table", Value: pyval.Strip(orEmpty("table"))},
			{Key: "mode", Value: pyval.Or(get("mode"), "append")},
			{Key: "post_sql", Value: orEmpty("postSql")},
		}})
	case pyval.Eq(destType, "sftp"):
		body = append(body, pyjson.KV{Key: "destination", Value: pyjson.Object{
			{Key: "type", Value: "sftp"},
			{Key: "connection_id", Value: get("connectionId")},
			{Key: "folder", Value: pyval.Strip(orEmpty("sftpFolder"))},
			{Key: "file_name", Value: pyval.Strip(orEmpty("sftpFileName"))},
			{Key: "format", Value: pyval.Or(get("sftpFormat"), "csv")},
		}})
	case pyval.Eq(destType, "s3"):
		body = append(body, pyjson.KV{Key: "destination", Value: pyjson.Object{
			{Key: "type", Value: "s3"},
			{Key: "connection_id", Value: get("connectionId")},
			{Key: "bucket", Value: pyval.Strip(orEmpty("s3Bucket"))},
			{Key: "key", Value: pyval.Strip(orEmpty("s3Key"))},
			{Key: "format", Value: pyval.Or(get("s3Format"), "parquet")},
			{Key: "partition_by", Value: pyval.Or(get("partitionBy"), []any{})},
		}})
	default:
		body = append(body, pyjson.KV{Key: "publish", Value: pyjson.Object{
			{Key: "name", Value: pyval.Strip(orEmpty("name"))},
			{Key: "project_id", Value: get("projectId")},
			{Key: "description", Value: orEmpty("description")},
			{Key: "overwrite", Value: pyval.Truthy(get("overwrite"))},
			{Key: "sort_keys", Value: pyval.Or(get("sortKeys"), []any{})},
		}})
		if pyval.Truthy(get("mirrorEnabled")) && pyval.Truthy(get("mirrorConnectionId")) {
			body = append(body, pyjson.KV{Key: "mirror", Value: pyjson.Object{
				{Key: "connection_id", Value: get("mirrorConnectionId")},
				{Key: "bucket", Value: pyval.Strip(orEmpty("mirrorBucket"))},
				{Key: "key", Value: pyval.Strip(orEmpty("mirrorKey"))},
			}})
		}
	}
	if tz := pyval.Strip(pyval.Str(pyval.Or(get("timezone"), ""))); tz != "" {
		for i, kv := range body {
			if kv.Key == "destination" || kv.Key == "email" || kv.Key == "mirror" {
				body[i].Value = kv.Value.(pyjson.Object).Set("timezone", tz)
			}
		}
	}
	return body
}

// definitionParts are definition.get("nodes") or [] and definition.get("edges") or [].
func definitionParts(definition any) (nodes, edges any) {
	return pyval.Or(pyval.Get(definition, "nodes"), []any{}), pyval.Or(pyval.Get(definition, "edges"), []any{})
}

// guard runs f as the Python code runs: a FlowResolveError, a Python exception or the
// resolver's own error comes back as the error.
func guard(f func()) error { return pyval.Catch(f) }

// ResolveOutputRequest is resolve_output_request: ONE Output node → its run's body. An *Error
// when its data chain has no resolvable source.
func ResolveOutputRequest(definition, node any, resolveDS DsResolver, defaultBucket string, mode Mode,
	scriptOutputs ScriptOutputs) (body pyjson.Object, err error) {
	err = guard(func() {
		nodes, edges := definitionParts(definition)
		r := newResolver(nodes, edges, resolveDS, mode, scriptOutputs)
		src, ops := r.chain(pyval.Index(node, "id"))
		if src == nil {
			raise(noSource(node))
		}
		body = outputBody(node, src, ops, defaultBucket)
	})
	return
}

// ScriptInputEdge is script_input_edge: the node feeding a Script (or Container) node's input,
// nil when it has none.
func ScriptInputEdge(definition any, nodeID any) (id any, err error) {
	err = guard(func() {
		_, edges := definitionParts(definition)
		id = inputOf(incoming(pyval.Iter(edges)), nodeID).left
	})
	return
}

// ScriptHasOutput is script_has_output: a data edge leaves the Script (or Container) node.
func ScriptHasOutput(definition any, nodeID any) (has bool, err error) {
	err = guard(func() {
		_, edges := definitionParts(definition)
		for _, e := range pyval.Iter(edges) {
			if pyval.Eq(pyval.Get(e, "source"), nodeID) && !isSeqEdge(e) {
				has = true
				return
			}
		}
	})
	return
}

// ResolveScriptInput is resolve_script_input: the chain ending at a Script (or Container)
// node's input as a run's body (bucket, input_key, operations); nil when it has no input.
func ResolveScriptInput(definition, node any, resolveDS DsResolver, defaultBucket string, mode Mode,
	scriptOutputs ScriptOutputs) (body pyjson.Object, err error) {
	err = guard(func() {
		nodes, edges := definitionParts(definition)
		upstream := inputOf(incoming(pyval.Iter(edges)), pyval.Index(node, "id")).left
		if !pyval.Truthy(upstream) {
			return
		}
		r := newResolver(nodes, edges, resolveDS, mode, scriptOutputs)
		src, ops := r.chain(upstream)
		if src == nil {
			label := pyval.Str(pyval.Or(pyval.Get(pyval.Or(pyval.Get(node, "data"), pyjson.Object{}), "label"), pyval.Get(node, "id")))
			raise(&Error{Message: "script " + label + ": no source with data upstream (or the source datasource has no snapshot)",
				Code: "script_no_source", Params: pyjson.Object{{Key: "script", Value: label}}})
		}
		body = pyjson.Object{{Key: "bucket", Value: pyval.Or(src.Bucket, defaultBucket)}, {Key: "input_key", Value: src.Key},
			{Key: "operations", Value: ops}}
	})
	return
}

// Chain is one item of ResolveOutputChains: the Output node, its run's body and the node id of
// every operation (the source's injected ones `<id>#head`).
type Chain struct {
	Node  any
	Body  pyjson.Object
	OpIDs []any
}

// ResolveOutputChains is resolve_output_chains: for every Output node (production, without
// samples) its body and the node ids of its operations; nodeIDs != nil keeps only those.
func ResolveOutputChains(definition any, resolveDS DsResolver, defaultBucket string, nodeIDs []any) (out []Chain, err error) {
	err = guard(func() {
		nodes, edges := definitionParts(definition)
		var outputs []any
		for _, n := range pyval.Iter(nodes) {
			if pyval.Eq(pyval.Get(n, "type"), "output") {
				outputs = append(outputs, n)
			}
		}
		if len(outputs) == 0 {
			raise(&Error{Message: "the flow has no Output nodes: there is nothing to export", Code: "no_outputs", Params: pyjson.Object{}})
		}
		if nodeIDs != nil {
			wanted := pyval.NewDict()
			for _, id := range nodeIDs {
				wanted.Set(id, true)
			}
			var kept []any
			for _, n := range outputs {
				if wanted.Has(pyval.Get(n, "id")) {
					kept = append(kept, n)
				}
			}
			outputs = kept
		}
		r := newResolver(nodes, edges, resolveDS, Mode{}, nil)
		for _, node := range outputs {
			src, ops, ids := r.chainWithIDs(pyval.Index(node, "id"))
			if src == nil {
				raise(noSource(node))
			}
			out = append(out, Chain{Node: node, Body: outputBody(node, src, ops, defaultBucket), OpIDs: ids})
		}
	})
	return
}

// BuildOutputRunRequests is build_output_run_requests: one body per Output node.
func BuildOutputRunRequests(definition any, resolveDS DsResolver, defaultBucket string, mode Mode) (out []pyjson.Object, err error) {
	err = guard(func() {
		nodes, _ := definitionParts(definition)
		var outputs []any
		for _, n := range pyval.Iter(nodes) {
			if pyval.Eq(pyval.Get(n, "type"), "output") {
				outputs = append(outputs, n)
			}
		}
		if len(outputs) == 0 {
			raise(&Error{Message: "the flow has no Output nodes: there is nothing to run", Code: "no_outputs", Params: pyjson.Object{}})
		}
		for _, n := range outputs {
			body, err := ResolveOutputRequest(definition, n, resolveDS, defaultBucket, mode, nil)
			pyval.Check(err)
			out = append(out, body)
		}
	})
	return
}

// ── the order of the action nodes ───────────────────────────────────────────

var actionTypes = []string{"refresh", "script", "container", "output", "runflow"}

var defaultPriority = map[string]int{"refresh": 0, "script": 1, "container": 1, "output": 1, "runflow": 2}

// ScriptsUpstream is _scripts_upstream: the Script and Container nodes a node reads data
// from, however far up, sorted (Python's is a set: its callers sort it or only test it).
// nodes is by node id (pyval.Key).
func ScriptsUpstream(nodeID any, nodes *pyval.Dict, edges []any) (found []any, err error) {
	err = guard(func() { found = scriptsUpstream(nodeID, nodes, edges) })
	return
}

func scriptsUpstream(nodeID any, nodes *pyval.Dict, edges []any) []any {
	into := pyval.NewDict()
	for _, e := range edges {
		if !isSeqEdge(e) {
			l, _ := into.SetDefault(pyval.Get(e, "target"), &[]any{}).(*[]any)
			*l = append(*l, pyval.Get(e, "source"))
		}
	}
	found := pyval.NewDict()
	stack := []any{nodeID}
	seen := map[string]bool{}
	for len(stack) > 0 {
		cur := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if seen[pyval.Key(cur)] {
			continue
		}
		seen[pyval.Key(cur)] = true
		parent := pyval.Get(pyval.Or(nodes.Get(cur), pyjson.Object{}), "parentNode")
		var ups []any
		if l, ok := into.Get(cur).(*[]any); ok {
			ups = append(ups, *l...)
		}
		if pyval.Truthy(parent) {
			ups = append(ups, parent)
		}
		for _, up := range ups {
			if inStrings(pyval.Get(pyval.Or(nodes.Get(up), pyjson.Object{}), "type"), StepNodeTypes...) {
				found.Set(up, true)
			} else {
				stack = append(stack, up)
			}
		}
	}
	out := append([]any{}, found.Keys()...)
	sortIDs(out)
	return out
}

// sortIDs is sorted() over node ids (a TypeError when they cannot be compared).
func sortIDs(ids []any) {
	var bad *pyval.Error
	sort.SliceStable(ids, func(i, j int) bool {
		if bad != nil {
			return false
		}
		less := false
		if err := pyval.Catch(func() { less = pyval.Less(ids[i], ids[j]) }); err != nil {
			bad, _ = err.(*pyval.Error)
		}
		return less
	})
	if bad != nil {
		panic(bad)
	}
}

// SortIDs is sorted() over node ids, for callers outside the package.
func SortIDs(ids []any) error { return guard(func() { sortIDs(ids) }) }

// NodesByID is {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}.
func NodesByID(nodes any) (d *pyval.Dict, err error) {
	err = guard(func() {
		d = pyval.NewDict()
		for _, n := range pyval.Iter(nodes) {
			if o, ok := n.(pyjson.Object); ok {
				if id, has := o.Get("id"); has {
					d.Set(id, n)
				}
			}
		}
	})
	return
}

// SequenceOrder is sequence_order: the action nodes (refresh/script/container/output/runflow)
// by the sequence edges (Kahn), the unconnected ones in the default order (refresh → output →
// runflow, then declaration order), the nodes of a cycle at the back.
func SequenceOrder(definition any) (out []any, err error) {
	err = guard(func() {
		nodes, edges := definitionParts(definition)
		var action []any
		for _, n := range pyval.Iter(nodes) {
			if inStrings(pyval.Get(n, "type"), actionTypes...) {
				action = append(action, n)
			}
		}
		byID, idx := pyval.NewDict(), pyval.NewDict()
		for _, n := range action {
			byID.Set(pyval.Index(n, "id"), n)
		}
		for i, n := range action {
			idx.Set(pyval.Index(n, "id"), i)
		}
		succ, indeg := pyval.NewDict(), pyval.NewDict()
		for _, n := range action {
			succ.Set(pyval.Index(n, "id"), &[]any{})
		}
		for _, n := range action {
			indeg.Set(pyval.Index(n, "id"), 0)
		}
		edgeList := pyval.Iter(edges)
		var pairs []pyval.Tuple
		for _, e := range edgeList {
			if pyval.Eq(pyval.Get(e, "targetHandle"), SeqTargetHandle) {
				pairs = append(pairs, pyval.Tuple{pyval.Get(e, "source"), pyval.Get(e, "target")})
			}
		}
		allNodes, err := NodesByID(nodes)
		pyval.Check(err)
		for _, n := range action {
			for _, s := range scriptsUpstream(pyval.Index(n, "id"), allNodes, edgeList) {
				pairs = append(pairs, pyval.Tuple{s, pyval.Index(n, "id")})
			}
		}
		done := map[string]bool{}
		for _, p := range pairs {
			k := pyval.Key(p)
			if done[k] {
				continue
			}
			done[k] = true
			s, t := p[0], p[1]
			if byID.Has(s) && byID.Has(t) && !pyval.Eq(s, t) {
				l := succ.Get(s).(*[]any)
				*l = append(*l, t)
				indeg.Set(t, indeg.Get(t).(int)+1)
			}
		}
		rank := func(id any) [2]int {
			typ, _ := pyval.Get(byID.Get(id), "type").(string)
			pr, ok := defaultPriority[typ]
			if !ok {
				pr = 9
			}
			return [2]int{pr, idx.Get(id).(int)}
		}
		byRank := func(ids []any) {
			sort.SliceStable(ids, func(i, j int) bool {
				a, b := rank(ids[i]), rank(ids[j])
				return a[0] < b[0] || (a[0] == b[0] && a[1] < b[1])
			})
		}
		var ready []any
		for _, id := range byID.Keys() {
			if indeg.Get(id).(int) == 0 {
				ready = append(ready, id)
			}
		}
		byRank(ready)
		seen := map[string]bool{}
		for len(ready) > 0 {
			id := ready[0]
			ready = ready[1:]
			if seen[pyval.Key(id)] {
				continue
			}
			seen[pyval.Key(id)] = true
			out = append(out, byID.Get(id))
			var newly []any
			for _, m := range *succ.Get(id).(*[]any) {
				indeg.Set(m, indeg.Get(m).(int)-1)
				if indeg.Get(m).(int) == 0 {
					newly = append(newly, m)
				}
			}
			if len(newly) > 0 {
				ready = append(ready, newly...)
				byRank(ready)
			}
		}
		var rest []any
		for _, id := range byID.Keys() {
			if !seen[pyval.Key(id)] {
				rest = append(rest, id)
			}
		}
		byRank(rest)
		for _, id := range rest {
			out = append(out, byID.Get(id))
		}
	})
	return
}

// DefaultSampleRows is default_sample_rows: the panel's automatic sample, capped.
func DefaultSampleRows(previewDefaultSampleRows int64) int64 {
	return int64(math.Min(float64(previewDefaultSampleRows), MaxSampleRows))
}

// ParseDefinition is json.loads(definition or "{}") as the resolver's callers read a flow.
func ParseDefinition(text string) (any, error) {
	if text == "" {
		text = "{}"
	}
	return pyjson.Loads(text)
}
