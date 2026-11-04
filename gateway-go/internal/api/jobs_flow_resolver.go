package api

import (
	"fmt"
	"math"
	"sort"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// services/flow_resolver.py: a flow's definition resolved into run requests on the server
// (the scheduler's and the queue's twin of the editor's resolveChain), and the order of its
// action nodes. A datasource source is resolved again to its current snapshot.

// flowResolveError is FlowResolveError: the definition cannot run.
type flowResolveError struct{ msg string }

func (e *flowResolveError) Error() string { return e.msg }

// dsResolver is DsResolver: (bucket, key) of a datasource's current snapshot, nil when none.
type dsResolver func(id any) *[2]any

var stepNodeTypes = map[string]bool{"script": true, "container": true}

const (
	devSampleMark  = "_dev_sample"
	maxSampleRows  = 100_000_000
	seqTargetHandl = "seq-in"
	seqSourceHandl = "seq-out"
)

var sourceFilterOperators = map[string]bool{"eq": true, "ne": true, "gt": true, "ge": true, "lt": true, "le": true, "in": true,
	"not_in": true, "between": true, "contains": true, "starts_with": true, "ends_with": true, "is_null": true, "is_not_null": true}

// sourceFilterOperations is source_filter_operations.
func sourceFilterOperations(data pyjson.Object) []any {
	filters, ok := data.Value("filters").([]any)
	if !ok {
		return nil
	}
	var ops []any
	for _, f := range filters {
		cond, ok := f.(pyjson.Object)
		if !ok {
			continue
		}
		column, isText := cond.Value("column").(string)
		operator, _ := cond.Value("operator").(string)
		if !isText || column == "" || !sourceFilterOperators[operator] {
			continue
		}
		params := pyjson.Object{{Key: "column", Value: column}, {Key: "operator", Value: operator}}
		if operator != "is_null" && operator != "is_not_null" {
			value := cond.Value("value")
			if value == nil {
				continue
			}
			list, isList := value.([]any)
			if (operator == "in" || operator == "not_in") && (!isList || len(list) == 0) {
				continue
			}
			if operator == "between" && (!isList || len(list) != 2) {
				continue
			}
			params = append(params, pyjson.KV{Key: "value", Value: value})
		}
		ops = append(ops, pyjson.Object{{Key: "type", Value: "filter"}, {Key: "params", Value: params}})
	}
	return ops
}

// devSampleOperation is dev_sample_operation.
func devSampleOperation(data pyjson.Object, defaultRows int64) any {
	limit := func(n int64) any {
		return pyjson.Object{{Key: "type", Value: "limit"}, {Key: "params", Value: pyjson.Object{{Key: "n", Value: min(n, maxSampleRows)},
			{Key: devSampleMark, Value: true}}}}
	}
	sample, ok := data.Value("sample").(pyjson.Object)
	if !ok {
		if defaultRows > 0 {
			return limit(defaultRows)
		}
		return nil
	}
	switch sample.Value("mode") {
	case "first":
		v := sample.Value("rows")
		var n int64
		if pyjson.Truthy(v) {
			var ok bool
			if n, ok = pyInt(v); !ok {
				return nil
			}
		}
		if n <= 0 {
			return nil
		}
		return limit(n)
	case "random":
		v := sample.Value("percent")
		var pct float64
		if pyjson.Truthy(v) {
			switch x := v.(type) {
			case int64:
				pct = float64(x)
			case float64:
				pct = x
			case bool:
				pct = 1
			case string:
				var err error
				if _, err = fmt.Sscan(strings.TrimSpace(x), &pct); err != nil {
					return nil
				}
			default:
				return nil
			}
		}
		if !(pct > 0 && pct < 100) || math.IsNaN(pct) {
			return nil
		}
		return pyjson.Object{{Key: "type", Value: "sample"}, {Key: "params", Value: pyjson.Object{{Key: "fraction", Value: pct / 100},
			{Key: "seed", Value: int64(42)}, {Key: devSampleMark, Value: true}}}}
	}
	return nil
}

// stripDevSampleOps is strip_dev_sample_ops: (the operations without any marked sample, how
// many were removed).
func stripDevSampleOps(operations []any) ([]any, int) {
	removed := 0
	var cleanOps func(ops any) []any
	var cleanParams func(params any) any
	cleanOps = func(ops any) []any {
		list, _ := ops.([]any)
		out := []any{}
		for _, op := range list {
			o, ok := op.(pyjson.Object)
			if !ok {
				out = append(out, op)
				continue
			}
			params := pyjson.Or(o.Value("params"), pyjson.Object{})
			if p, isDict := params.(pyjson.Object); isDict && pyjson.Truthy(p.Value(devSampleMark)) {
				removed++
				continue
			}
			out = append(out, append(pyjson.Object(nil), o...).Set("params", cleanParams(params)))
		}
		return out
	}
	cleanParams = func(params any) any {
		p, ok := params.(pyjson.Object)
		if !ok {
			return params
		}
		n := append(pyjson.Object(nil), p...)
		for _, key := range []string{"right", "driver"} {
			if ref, ok := n.Value(key).(pyjson.Object); ok {
				if _, has := ref.Get("operations"); has {
					n = n.Set(key, append(pyjson.Object(nil), ref...).Set("operations", cleanOps(ref.Value("operations"))))
				}
			}
		}
		if body, ok := n.Value("body").([]any); ok {
			n = n.Set("body", cleanOps(body))
		}
		return n
	}
	return cleanOps(operations), removed
}

func isSeqEdge(e pyjson.Object) bool {
	return e.Value("targetHandle") == seqTargetHandl || e.Value("sourceHandle") == seqSourceHandl
}

// key is a node id as a dict key.
func nodeKey(v any) string { return pyjson.Repr(v) }

// incoming is _incoming: target → {left, right} of the data edges.
func incoming(edges []pyjson.Object) map[string]map[string]any {
	m := map[string]map[string]any{}
	for _, e := range edges {
		if isSeqEdge(e) {
			continue
		}
		handle := pyjson.Or(e.Value("targetHandle"), "left")
		k := nodeKey(e.Value("target"))
		if m[k] == nil {
			m[k] = map[string]any{}
		}
		side := "left"
		if handle == "right" {
			side = "right"
		}
		m[k][side] = e.Value("source")
	}
	return m
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

type flowResolver struct {
	byID          map[string]pyjson.Object
	order         []string // by_id's keys, in insertion order
	edges         []pyjson.Object
	inc           map[string]map[string]any
	resolveDS     dsResolver
	dev           bool
	defaultRows   int64
	scriptOutputs map[string]*[2]any
	inProgress    map[string]bool
}

func newFlowResolver(nodes, edges []pyjson.Object, resolveDS dsResolver, engineMode string, defaultRows int64,
	scriptOutputs map[string]*[2]any) *flowResolver {
	r := &flowResolver{byID: map[string]pyjson.Object{}, edges: edges, inc: incoming(edges), resolveDS: resolveDS,
		dev: engineMode == "development", scriptOutputs: scriptOutputs, inProgress: map[string]bool{}}
	for _, n := range nodes {
		k := nodeKey(n.Value("id"))
		if _, seen := r.byID[k]; !seen {
			r.order = append(r.order, k)
		}
		r.byID[k] = n
	}
	if r.dev {
		r.defaultRows = min(defaultRows, maxSampleRows)
	}
	return r
}

func (r *flowResolver) once(call string, nodeID any, f func() error) error {
	if r.inProgress[call] {
		node := r.byID[nodeKey(nodeID)]
		label := pyjson.Str(pyjson.Or(nodeData(node).Value("label"), nodeID))
		return &flowResolveError{fmt.Sprintf("the flow has a loop through the node «%s»: remove the edge that closes it", label)}
	}
	r.inProgress[call] = true
	defer delete(r.inProgress, call)
	return f()
}

// chain is chain: (the resolved source, the IR operations) ending at target.
func (r *flowResolver) chain(target any) (*[2]any, []any, error) {
	var src *[2]any
	var ops []any
	err := r.once("chain|"+nodeKey(target), target, func() error {
		var err error
		src, ops, err = r.chainOf(target)
		return err
	})
	return src, ops, err
}

func (r *flowResolver) chainOf(targetID any) (*[2]any, []any, error) {
	target, has := r.byID[nodeKey(targetID)]
	if has && pyjson.Truthy(target.Value("parentNode")) {
		parent := target.Value("parentNode")
		var upSrc *[2]any
		var upOps []any
		if up := r.inc[nodeKey(parent)]["left"]; pyjson.Truthy(up) {
			var err error
			if upSrc, upOps, err = r.chain(up); err != nil {
				return nil, nil, err
			}
		}
		op, err := r.operationFor(parent, targetID)
		if err != nil {
			return nil, nil, err
		}
		return upSrc, append(append([]any{}, upOps...), op), nil
	}
	var opIDs []any
	seen := map[string]bool{}
	var source *[2]any
	var head []any
	cur := targetID
	for pyjson.Truthy(cur) && !seen[nodeKey(cur)] {
		seen[nodeKey(cur)] = true
		node, ok := r.byID[nodeKey(cur)]
		if !ok || len(node) == 0 {
			break
		}
		t := node.Value("type")
		if s, isText := t.(string); isText && stepNodeTypes[s] {
			source = r.scriptOutputs[nodeKey(cur)]
			break
		}
		if t == "source" {
			source = resolveSource(node, r.resolveDS)
			data := nodeData(node)
			head = sourceFilterOperations(data)
			if r.dev {
				if op := devSampleOperation(data, r.defaultRows); op != nil {
					head = append(head, op)
				}
			}
			break
		}
		if t == "operation" || t == "foreach" {
			opIDs = append(opIDs, cur)
		}
		cur = r.inc[nodeKey(cur)]["left"]
	}
	ops := append([]any{}, head...)
	for i := len(opIDs) - 1; i >= 0; i-- {
		op, err := r.operationFor(opIDs[i], nil)
		if err != nil {
			return nil, nil, err
		}
		ops = append(ops, op)
	}
	return source, ops, nil
}

// resolveSource is _resolve_source.
func resolveSource(node pyjson.Object, resolveDS dsResolver) *[2]any {
	data := nodeData(node)
	if id := data.Value("datasourceId"); id != nil {
		if got := resolveDS(id); got != nil && pyjson.Truthy(got[1]) {
			return got
		}
		return nil
	}
	if k := data.Value("parquetKey"); pyjson.Truthy(k) {
		return &[2]any{data.Value("bucket"), k}
	}
	return nil
}

func (r *flowResolver) operationFor(nodeID, until any) (any, error) {
	var op any
	err := r.once("operation|"+nodeKey(nodeID)+"|"+nodeKey(until), nodeID, func() error {
		var err error
		op, err = r.buildOperation(nodeID, until)
		return err
	})
	return op, err
}

func (r *flowResolver) buildOperation(nodeID, until any) (any, error) {
	node, ok := r.byID[nodeKey(nodeID)]
	if !ok {
		return nil, fmt.Errorf("KeyError: %s", nodeKey(nodeID))
	}
	data := nodeData(node)
	params := append(pyjson.Object(nil), pyjson.Obj(pyjson.Or(data.Value("params"), pyjson.Object{}))...)
	opType := data.Value("opType")
	sourced := func(src *[2]any, ops []any) pyjson.Object {
		return pyjson.Object{{Key: "source", Value: pyjson.Object{{Key: "bucket", Value: src[0]}, {Key: "key", Value: src[1]}}},
			{Key: "operations", Value: ops}}
	}
	if opType == "join" || opType == "union" {
		if right := r.inc[nodeKey(nodeID)]["right"]; pyjson.Truthy(right) {
			src, ops, err := r.chain(right)
			if err != nil {
				return nil, err
			}
			if src != nil {
				params = params.Set("right", sourced(src, ops))
			}
		}
	}
	if node.Value("type") == "foreach" {
		if driver := r.inc[nodeKey(nodeID)]["right"]; pyjson.Truthy(driver) {
			src, ops, err := r.chain(driver)
			if err != nil {
				return nil, err
			}
			if src != nil {
				params = params.Set("driver", sourced(src, ops))
			}
		}
		body, err := r.containerBody(nodeID, until)
		if err != nil {
			return nil, err
		}
		params = params.Set("body", body)
	}
	return pyjson.Object{{Key: "type", Value: opType}, {Key: "params", Value: params}}, nil
}

func (r *flowResolver) containerBody(containerID, until any) ([]any, error) {
	var children []pyjson.Object
	for _, k := range r.order {
		if n := r.byID[k]; pyjson.Equal(n.Value("parentNode"), containerID) && n.Value("parentNode") != nil {
			children = append(children, n)
		}
	}
	if len(children) == 0 {
		return []any{}, nil
	}
	childIDs := map[string]bool{}
	for _, c := range children {
		childIDs[nodeKey(c.Value("id"))] = true
	}
	next := map[string]any{}
	for _, e := range r.edges {
		if childIDs[nodeKey(e.Value("source"))] && childIDs[nodeKey(e.Value("target"))] && pyjson.Or(e.Value("targetHandle"), "left") == "left" {
			next[nodeKey(e.Value("source"))] = e.Value("target")
		}
	}
	var cur any
	for _, c := range children {
		left, has := r.inc[nodeKey(c.Value("id"))]["left"]
		if !has || !childIDs[nodeKey(left)] {
			cur = c.Value("id")
			break
		}
	}
	ops := []any{}
	seen := map[string]bool{}
	for pyjson.Truthy(cur) && !seen[nodeKey(cur)] {
		seen[nodeKey(cur)] = true
		op, err := r.operationFor(cur, nil)
		if err != nil {
			return nil, err
		}
		ops = append(ops, op)
		if until != nil && pyjson.Equal(cur, until) {
			break
		}
		cur = next[nodeKey(cur)]
	}
	return ops, nil
}

func textOrEmpty(v any) string {
	if !pyjson.Truthy(v) {
		return ""
	}
	return pyjson.Str(v)
}

// outputLabel is _output_label.
func outputLabel(node pyjson.Object) string {
	d := nodeData(node)
	or := func(k string) string {
		if v := d.Value(k); pyjson.Truthy(v) {
			return pyjson.Str(v)
		}
		return "…"
	}
	switch pyjson.Or(d.Value("destType"), "datasource") {
	case "database":
		return "table " + or("table")
	case "s3":
		return "S3 " + or("s3Key")
	case "sftp":
		return "SFTP " + or("sftpFileName")
	}
	return "datasource “" + or("name") + "”"
}

// outputBody is _output_body: the run body (RunCreate's shape) of one Output node.
func outputBody(node pyjson.Object, source *[2]any, operations []any, defaultBucket string) pyjson.Object {
	d := nodeData(node)
	strip := func(k string) string { return pyStripText(textOrEmpty(d.Value(k))) }
	body := pyjson.Object{{Key: "bucket", Value: pyjson.Or(source[0], defaultBucket)}, {Key: "input_key", Value: source[1]},
		{Key: "operations", Value: operations}}
	part := ""
	switch pyjson.Or(d.Value("destType"), "datasource") {
	case "email":
		part = "email"
		body = append(body, pyjson.KV{Key: "email", Value: pyjson.Object{{Key: "connection_id", Value: d.Value("connectionId")},
			{Key: "to", Value: pyjson.Or(d.Value("emailTo"), []any{})}, {Key: "cc", Value: pyjson.Or(d.Value("emailCc"), []any{})},
			{Key: "subject", Value: pyjson.Or(d.Value("emailSubject"), "")}, {Key: "body", Value: pyjson.Or(d.Value("emailBody"), "")},
			{Key: "body_is_html", Value: pyjson.Truthy(d.Value("emailHtml"))}, {Key: "attachment_name", Value: strip("attachmentName")},
			{Key: "attachment_format", Value: pyjson.Or(d.Value("attachmentFormat"), "xlsx")},
			{Key: "stop_on_failure", Value: d.Value("stopOnFailure") != false}}})
	case "database":
		part = "destination"
		body = append(body, pyjson.KV{Key: "destination", Value: pyjson.Object{{Key: "type", Value: "database"},
			{Key: "connection_id", Value: d.Value("connectionId")}, {Key: "table", Value: strip("table")},
			{Key: "mode", Value: pyjson.Or(d.Value("mode"), "append")}, {Key: "post_sql", Value: pyjson.Or(d.Value("postSql"), "")}}})
	case "sftp":
		part = "destination"
		body = append(body, pyjson.KV{Key: "destination", Value: pyjson.Object{{Key: "type", Value: "sftp"},
			{Key: "connection_id", Value: d.Value("connectionId")}, {Key: "folder", Value: strip("sftpFolder")},
			{Key: "file_name", Value: strip("sftpFileName")}, {Key: "format", Value: pyjson.Or(d.Value("sftpFormat"), "csv")}}})
	case "s3":
		part = "destination"
		body = append(body, pyjson.KV{Key: "destination", Value: pyjson.Object{{Key: "type", Value: "s3"},
			{Key: "connection_id", Value: d.Value("connectionId")}, {Key: "bucket", Value: strip("s3Bucket")}, {Key: "key", Value: strip("s3Key")},
			{Key: "format", Value: pyjson.Or(d.Value("s3Format"), "parquet")}, {Key: "partition_by", Value: pyjson.Or(d.Value("partitionBy"), []any{})}}})
	default:
		body = append(body, pyjson.KV{Key: "publish", Value: pyjson.Object{{Key: "name", Value: strip("name")},
			{Key: "project_id", Value: d.Value("projectId")}, {Key: "description", Value: pyjson.Or(d.Value("description"), "")},
			{Key: "overwrite", Value: pyjson.Truthy(d.Value("overwrite"))}, {Key: "sort_keys", Value: pyjson.Or(d.Value("sortKeys"), []any{})}}})
		if pyjson.Truthy(d.Value("mirrorEnabled")) && pyjson.Truthy(d.Value("mirrorConnectionId")) {
			body = append(body, pyjson.KV{Key: "mirror", Value: pyjson.Object{{Key: "connection_id", Value: d.Value("mirrorConnectionId")},
				{Key: "bucket", Value: strip("mirrorBucket")}, {Key: "key", Value: strip("mirrorKey")}}})
		}
	}
	if tz := pyStripText(textOrEmpty(d.Value("timezone"))); tz != "" {
		for i, kv := range body {
			if kv.Key == "destination" || kv.Key == "email" || kv.Key == "mirror" {
				body[i].Value = kv.Value.(pyjson.Object).Set("timezone", tz)
			}
		}
	}
	_ = part
	return body
}

// resolveOutputRequest is resolve_output_request.
func resolveOutputRequest(definition pyjson.Object, node pyjson.Object, resolveDS dsResolver, defaultBucket, engineMode string,
	defaultRows int64, scriptOutputs map[string]*[2]any) (pyjson.Object, error) {
	r := newFlowResolver(objectList(pyjson.Or(definition.Value("nodes"), []any{})), objectList(pyjson.Or(definition.Value("edges"), []any{})),
		resolveDS, engineMode, defaultRows, scriptOutputs)
	source, ops, err := r.chain(node.Value("id"))
	if err != nil {
		return nil, err
	}
	if source == nil {
		name := pyStripText(textOrEmpty(outputNameOf(node)))
		_ = name
		return nil, &flowResolveError{fmt.Sprintf("output %s: no source with data upstream (or the source datasource has no snapshot)",
			outputLabel(node))}
	}
	return outputBody(node, source, ops, defaultBucket), nil
}

func outputNameOf(node pyjson.Object) any {
	d := nodeData(node)
	switch pyjson.Or(d.Value("destType"), "datasource") {
	case "database":
		return d.Value("table")
	case "s3":
		return d.Value("s3Key")
	case "sftp":
		return d.Value("sftpFileName")
	}
	return d.Value("name")
}

var actionPriority = map[string]int{"refresh": 0, "script": 1, "container": 1, "output": 1, "runflow": 2}

// scriptsUpstream is _scripts_upstream.
func scriptsUpstream(nodeID any, nodes map[string]pyjson.Object, edges []pyjson.Object) map[string]bool {
	into := map[string][]any{}
	for _, e := range edges {
		if !isSeqEdge(e) {
			into[nodeKey(e.Value("target"))] = append(into[nodeKey(e.Value("target"))], e.Value("source"))
		}
	}
	found := map[string]bool{}
	stack := []any{nodeID}
	seen := map[string]bool{}
	for len(stack) > 0 {
		cur := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if seen[nodeKey(cur)] {
			continue
		}
		seen[nodeKey(cur)] = true
		ups := append([]any{}, into[nodeKey(cur)]...)
		if parent := nodes[nodeKey(cur)].Value("parentNode"); pyjson.Truthy(parent) {
			ups = append(ups, parent)
		}
		for _, up := range ups {
			if t, ok := nodes[nodeKey(up)].Value("type").(string); ok && stepNodeTypes[t] {
				found[nodeKey(up)] = true
			} else {
				stack = append(stack, up)
			}
		}
	}
	return found
}

// sequenceOrder is sequence_order: the action nodes by the sequence edges (Kahn), the free
// ones in the default order, the ones in a cycle at the back.
func sequenceOrder(definition pyjson.Object) []pyjson.Object {
	nodes := objectList(pyjson.Or(definition.Value("nodes"), []any{}))
	edges := objectList(pyjson.Or(definition.Value("edges"), []any{}))
	byID := map[string]pyjson.Object{}
	idx := map[string]int{}
	var ids []string
	i := 0
	for _, n := range nodes {
		t, _ := n.Value("type").(string)
		if _, ok := actionPriority[t]; !ok {
			continue
		}
		k := nodeKey(n.Value("id"))
		if _, seen := byID[k]; !seen {
			ids = append(ids, k)
		}
		byID[k], idx[k] = n, i
		i++
	}
	succ := map[string][]string{}
	indeg := map[string]int{}
	for _, k := range ids {
		succ[k], indeg[k] = []string{}, 0
	}
	type pair struct{ s, t string }
	var pairs []pair
	for _, e := range edges {
		if e.Value("targetHandle") == seqTargetHandl {
			pairs = append(pairs, pair{nodeKey(e.Value("source")), nodeKey(e.Value("target"))})
		}
	}
	all := map[string]pyjson.Object{}
	for _, n := range nodes {
		if _, has := n.Get("id"); has {
			all[nodeKey(n.Value("id"))] = n
		}
	}
	for _, k := range ids {
		ups := scriptsUpstream(byID[k].Value("id"), all, edges)
		var keys []string
		for u := range ups {
			keys = append(keys, u)
		}
		sort.Strings(keys) // a set in Python: its order does not reach the result (dedup, then Kahn by rank)
		for _, u := range keys {
			pairs = append(pairs, pair{u, k})
		}
	}
	done := map[pair]bool{}
	for _, p := range pairs {
		if done[p] {
			continue
		}
		done[p] = true
		_, okS := byID[p.s]
		_, okT := byID[p.t]
		if okS && okT && p.s != p.t {
			succ[p.s] = append(succ[p.s], p.t)
			indeg[p.t]++
		}
	}
	rank := func(k string) [2]int {
		t, _ := byID[k].Value("type").(string)
		pr, ok := actionPriority[t]
		if !ok {
			pr = 9
		}
		return [2]int{pr, idx[k]}
	}
	less := func(a, b string) bool {
		ra, rb := rank(a), rank(b)
		return ra[0] < rb[0] || (ra[0] == rb[0] && ra[1] < rb[1])
	}
	var ready []string
	for _, k := range ids {
		if indeg[k] == 0 {
			ready = append(ready, k)
		}
	}
	sort.SliceStable(ready, func(i, j int) bool { return less(ready[i], ready[j]) })
	var out []pyjson.Object
	seen := map[string]bool{}
	for len(ready) > 0 {
		k := ready[0]
		ready = ready[1:]
		if seen[k] {
			continue
		}
		seen[k] = true
		out = append(out, byID[k])
		var newly []string
		for _, m := range succ[k] {
			indeg[m]--
			if indeg[m] == 0 {
				newly = append(newly, m)
			}
		}
		if len(newly) > 0 {
			ready = append(ready, newly...)
			sort.SliceStable(ready, func(i, j int) bool { return less(ready[i], ready[j]) })
		}
	}
	var rest []string
	for _, k := range ids {
		if !seen[k] {
			rest = append(rest, k)
		}
	}
	sort.SliceStable(rest, func(i, j int) bool { return less(rest[i], rest[j]) })
	for _, k := range rest {
		out = append(out, byID[k])
	}
	return out
}
