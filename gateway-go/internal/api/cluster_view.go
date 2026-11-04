package api

// services/cluster_view.py snapshot: Administration → Cluster on Kubernetes, read-only. The
// pods of the release's namespace and of the namespaces where users' code runs, their state,
// CPU and memory against their limits (metrics-server), the warning events. The values come
// from the Kubernetes API as JSON: every step reads them as the Python code does, and where
// Python raises (an AttributeError on a value that is not a dict, a TypeError…) the error
// here is a 500 too.

import (
	"context"
	"errors"
	"fmt"
	"math"
	"math/big"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

const (
	cvLabelScriptRun    = "tabularia.io/script-run"
	cvLabelContainerRun = "tabularia.io/container-run"
	cvLabelEnv          = "tabularia.io/environment"
	cvMaxEvents         = 40
)

var (
	cvCPU    = regexp.MustCompile(`^(\d+(?:\.\d+)?)(n|u|m)?$`)
	cvMemory = regexp.MustCompile(`^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|k|M|G|T|e\d+)?$`)
)

// cvType is type(v).__name__ of a JSON value.
func cvType(v any) string {
	switch v.(type) {
	case nil:
		return "NoneType"
	case bool:
		return "bool"
	case int64, pyjson.Int:
		return "int"
	case float64:
		return "float"
	case string:
		return "str"
	case pyjson.Object:
		return "dict"
	case []any:
		return "list"
	}
	return "object"
}

// cvGet is v.get(key): an AttributeError when v is not a dict.
func cvGet(v any, key string) (any, error) {
	o, ok := v.(pyjson.Object)
	if !ok {
		return nil, fmt.Errorf("'%s' object has no attribute 'get'", cvType(v))
	}
	return o.Value(key), nil
}

// orDict is `v or {}`.
func orDict(v any) any {
	if pyjson.Truthy(v) {
		return v
	}
	return pyjson.Object{}
}

// cvGetOr is `(v.get(key) or {})`.
func cvGetOr(v any, key string) (any, error) {
	x, err := cvGet(v, key)
	return orDict(x), err
}

// cvIter is the items a `for` walks over a value (`x or []`): a list's, a dict's keys, a
// text's characters; a TypeError for a number.
func cvIter(v any) ([]any, error) {
	if !pyjson.Truthy(v) {
		return nil, nil
	}
	switch x := v.(type) {
	case []any:
		return x, nil
	case pyjson.Object:
		out := make([]any, len(x))
		for i, kv := range x {
			out[i] = kv.Key
		}
		return out, nil
	case string:
		var out []any
		for _, r := range x {
			out = append(out, string(r))
		}
		return out, nil
	}
	return nil, fmt.Errorf("'%s' object is not iterable", cvType(v))
}

// cvLen is len(v) of `x or []`.
func cvLen(v any) (int, error) {
	if !pyjson.Truthy(v) {
		return 0, nil
	}
	switch x := v.(type) {
	case []any:
		return len(x), nil
	case pyjson.Object:
		return len(x), nil
	case string:
		return len([]rune(x)), nil
	}
	return 0, fmt.Errorf("object of type '%s' has no len()", cvType(v))
}

var cvIntText = regexp.MustCompile(`^[+-]?\d+(_\d+)*$`)

// cvPyInt is int(v): a ValueError or a TypeError as errValue, an OverflowError apart.
func cvPyInt(v any) (any, error) {
	switch x := v.(type) {
	case bool:
		if x {
			return int64(1), nil
		}
		return int64(0), nil
	case int64, pyjson.Int:
		return x, nil
	case float64:
		if math.IsNaN(x) {
			return nil, errValue
		}
		if math.IsInf(x, 0) {
			return nil, errors.New("cannot convert float infinity to integer")
		}
		return floatInt(math.Trunc(x)), nil
	case string:
		s := strings.TrimSpace(x)
		if !cvIntText.MatchString(s) {
			return nil, errValue
		}
		s = strings.ReplaceAll(s, "_", "")
		if n, err := strconv.ParseInt(s, 10, 64); err == nil {
			return n, nil
		}
		b, _ := new(big.Int).SetString(s, 10)
		return pyjson.Int(b.String()), nil
	}
	return nil, errValue
}

var errValue = errors.New("int(): not a number")

// floatInt is int(f) of a whole float: an int64, or the digits past it.
func floatInt(f float64) any {
	if f >= -9.223372036854775808e18 && f < 9.223372036854775808e18 {
		return int64(f)
	}
	b, _ := new(big.Float).SetFloat64(f).Int(nil)
	return pyjson.Int(b.String())
}

// cvInt is cluster_view._int: int(value), None on a TypeError or a ValueError.
func cvInt(v any) (any, error) {
	n, err := cvPyInt(v)
	if errors.Is(err, errValue) {
		return nil, nil
	}
	return n, err
}

// cvText is str(q or "").strip().
func cvText(q any) string {
	if !pyjson.Truthy(q) {
		return ""
	}
	return strings.TrimSpace(pyjson.Str(q))
}

// cpuMillis is cpu_millis: a CPU quantity in millicores; ok=false for None.
func cpuMillis(q any) (float64, bool) {
	m := cvCPU.FindStringSubmatch(cvText(q))
	if m == nil {
		return 0, false
	}
	value, _ := strconv.ParseFloat(m[1], 64)
	switch m[2] {
	case "n":
		return value / 1e6, true
	case "u":
		return value / 1e3, true
	case "m":
		return value, true
	}
	return value * 1000, true
}

// memoryBytes is memory_bytes: a memory quantity in bytes (an int); nil for None.
func memoryBytes(q any) (any, error) {
	m := cvMemory.FindStringSubmatch(cvText(q))
	if m == nil {
		return nil, nil
	}
	value, _ := strconv.ParseFloat(m[1], 64)
	unit := m[2]
	if strings.HasPrefix(unit, "e") {
		n, _ := strconv.Atoi(unit[1:])
		if n > 308 { // 10 ** n does not fit a float
			return nil, errors.New("int too large to convert to float")
		}
		f := value * math.Pow10(n)
		if math.IsInf(f, 0) {
			return nil, errors.New("cannot convert float infinity to integer")
		}
		return floatInt(math.Trunc(f)), nil
	}
	factor := map[string]float64{"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[unit]
	if factor == 0 {
		factor = 1
	}
	return floatInt(math.Trunc(value * factor)), nil
}

func cvAddInt(a, b any) any {
	x, _ := a.(int64)
	y, ok := b.(int64)
	if !ok {
		return a // past int64: not summed (an absurd quantity)
	}
	return x + y
}

// clusterNamespace is one namespace the page looks at, and its role.
type clusterNamespace struct{ name, role string }

// clusterReleaseNamespace is release_namespace.
func clusterReleaseNamespace(d *Deps) string {
	k := d.Config.Jobs.Kube
	if k.Namespace != "" {
		return k.Namespace
	}
	raw, err := os.ReadFile(k.NamespaceFile)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// clusterNamespaces is namespaces(): Tabularia's first, none twice, none empty.
func clusterNamespaces(d *Deps) []clusterNamespace {
	s := scriptcfg.Get()
	out := []clusterNamespace{{clusterReleaseNamespace(d), "tabularia"}}
	or := func(a, b string) string {
		if a != "" {
			return a
		}
		return b
	}
	if s.Scripts.Enabled {
		out = append(out, clusterNamespace{s.Scripts.Namespace, "scripts"},
			clusterNamespace{or(s.Scripts.TrialNamespace, s.Scripts.Namespace), "script-trials"},
			clusterNamespace{or(s.Scripts.BuildNamespace, s.Scripts.Namespace), "script-builds"})
	}
	if s.Containers.Enabled {
		out = append(out, clusterNamespace{s.Containers.Namespace, "containers"},
			clusterNamespace{or(s.Containers.TrialNamespace, s.Containers.Namespace), "container-trials"})
	}
	seen := map[string]bool{}
	var kept []clusterNamespace
	for _, n := range out {
		if n.name != "" && !seen[n.name] {
			seen[n.name] = true
			kept = append(kept, n)
		}
	}
	return kept
}

// cvUsage is a pod's CPU (millicores: an int 0, or a float) and memory (bytes).
type cvUsage struct{ cpu, memory any }

// cvKey is a value as a dict's key; ok=false where Python says unhashable.
func cvKey(v any) (string, bool) {
	switch x := v.(type) {
	case nil:
		return "n", true
	case string:
		return "s" + x, true
	case bool, int64, float64, pyjson.Int:
		f, ok := cvNumber(x)
		if !ok {
			return "i" + string(x.(pyjson.Int)), true
		}
		return "f" + strconv.FormatFloat(f, 'g', -1, 64), true
	}
	return "", false
}

func cvNumber(v any) (float64, bool) {
	switch x := v.(type) {
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	case int64:
		return float64(x), true
	case float64:
		return x, true
	}
	return 0, false
}

// cvNamespaceResult is what _namespace gives: the pods, the usage by pod name (nil: no
// metrics), the warning events and the error.
type cvNamespaceResult struct {
	pods, events []any
	usage        map[string]cvUsage
	hasUsage     bool
	err          string
}

// cvGetAPI is _get: None for 403 and 404 (no right there, no metrics-server).
func cvGetAPI(ctx context.Context, path string) (any, bool, error) {
	v, err := kube.Call(ctx, kubeAPI(), http.MethodGet, path, nil, false)
	if s := kube.Status(err); s == 403 || s == 404 {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return v, true, nil
}

// cvNamespace is _namespace: its three reads side by side (asyncio.gather: the first
// failure that comes back is the one raised; a KubeError is the namespace's error, any
// other a 500).
func cvNamespace(ctx context.Context, ns string) (cvNamespaceResult, error) {
	paths := []string{"/api/v1/namespaces/" + ns + "/pods", "/apis/metrics.k8s.io/v1beta1/namespaces/" + ns + "/pods",
		"/api/v1/namespaces/" + ns + "/events?fieldSelector=type%3DWarning"}
	type answer struct {
		v   any
		got bool
	}
	answers := make([]answer, 3)
	var first error
	var mu sync.Mutex
	var wg sync.WaitGroup
	for i, p := range paths {
		wg.Add(1)
		go func() {
			defer wg.Done()
			v, got, err := cvGetAPI(ctx, p)
			mu.Lock()
			defer mu.Unlock()
			answers[i] = answer{v, got}
			if err != nil && first == nil {
				first = err
			}
		}()
	}
	wg.Wait()
	var out cvNamespaceResult
	if first != nil {
		var ke *kube.Error
		if errors.As(first, &ke) {
			out.err = ke.Message
			return out, nil
		}
		return out, first
	}
	pods, metrics, events := answers[0], answers[1], answers[2]
	if !pods.got || pods.v == nil {
		out.err = "Tabularia may not read the pods of " + ns
		return out, nil
	}
	if metrics.got && metrics.v != nil {
		out.hasUsage, out.usage = true, map[string]cvUsage{}
		itemsV, err := cvGet(metrics.v, "items")
		if err != nil {
			return out, err
		}
		items, err := cvIter(itemsV)
		if err != nil {
			return out, err
		}
		for _, item := range items {
			containersV, err := cvGet(item, "containers")
			if err != nil {
				return out, err
			}
			containers, err := cvIter(containersV)
			if err != nil {
				return out, err
			}
			var cpu any = int64(0)
			for _, c := range containers {
				u, err := cvGetOr(c, "usage")
				if err != nil {
					return out, err
				}
				q, err := cvGet(u, "cpu")
				if err != nil {
					return out, err
				}
				if m, ok := cpuMillis(q); ok && m != 0 {
					f, _ := cvNumber(cpu)
					cpu = f + m
				}
			}
			var memory any = int64(0)
			for _, c := range containers {
				u, err := cvGetOr(c, "usage")
				if err != nil {
					return out, err
				}
				q, err := cvGet(u, "memory")
				if err != nil {
					return out, err
				}
				b, err := memoryBytes(q)
				if err != nil {
					return out, err
				}
				if pyjson.Truthy(b) {
					memory = cvAddInt(memory, b)
				}
			}
			meta, err := cvGetOr(item, "metadata")
			if err != nil {
				return out, err
			}
			name, err := cvGet(meta, "name")
			if err != nil {
				return out, err
			}
			key, ok := cvKey(name)
			if !ok {
				return out, fmt.Errorf("unhashable type: '%s'", cvType(name))
			}
			out.usage[key] = cvUsage{cpu, memory}
		}
	}
	itemsV, err := cvGet(pods.v, "items")
	if err != nil {
		return out, err
	}
	if out.pods, err = cvIter(itemsV); err != nil {
		return out, err
	}
	if out.pods == nil {
		out.pods = []any{}
	}
	if events.got {
		evV, err := cvGetOr(orDict(events.v), "items")
		if err != nil {
			return out, err
		}
		if !pyjson.Truthy(evV) {
			out.events = nil
		} else if out.events, err = cvIter(evV); err != nil {
			return out, err
		}
	}
	return out, nil
}

// cvState is _state: what the pod is doing, its restarts, ready «x/y».
func cvState(pod any) (any, any, string, error) {
	status, err := cvGetOr(pod, "status")
	if err != nil {
		return nil, nil, "", err
	}
	statusesV, err := cvGet(status, "containerStatuses")
	if err != nil {
		return nil, nil, "", err
	}
	statuses, err := cvIter(statusesV)
	if err != nil {
		return nil, nil, "", err
	}
	var restarts any = int64(0)
	for _, c := range statuses {
		n, err := cvGet(c, "restartCount")
		if err != nil {
			return nil, nil, "", err
		}
		if !pyjson.Truthy(n) {
			n = int64(0)
		}
		i, err := cvPyInt(n)
		if err != nil {
			return nil, nil, "", err
		}
		restarts = cvAddInt(restarts, i)
	}
	readyCount := 0
	for _, c := range statuses {
		r, err := cvGet(c, "ready")
		if err != nil {
			return nil, nil, "", err
		}
		if pyjson.Truthy(r) {
			readyCount++
		}
	}
	total := len(statuses)
	if total == 0 {
		spec, err := cvGetOr(pod, "spec")
		if err != nil {
			return nil, nil, "", err
		}
		cs, err := cvGet(spec, "containers")
		if err != nil {
			return nil, nil, "", err
		}
		if total, err = cvLen(cs); err != nil {
			return nil, nil, "", err
		}
	}
	ready := fmt.Sprintf("%d/%d", readyCount, total)
	for _, c := range statuses {
		state, err := cvGetOr(c, "state")
		if err != nil {
			return nil, nil, "", err
		}
		waiting, err := cvGetOr(state, "waiting")
		if err != nil {
			return nil, nil, "", err
		}
		reason, err := cvGet(waiting, "reason")
		if err != nil {
			return nil, nil, "", err
		}
		if !(reason == nil || pyjson.Equal(reason, "ContainerCreating") || pyjson.Equal(reason, "PodInitializing")) {
			w, _ := cvGet(state, "waiting")
			r, err := cvGet(w, "reason")
			return r, restarts, ready, err
		}
		lastState, err := cvGetOr(c, "lastState")
		if err != nil {
			return nil, nil, "", err
		}
		last, err := cvGetOr(lastState, "terminated")
		if err != nil {
			return nil, nil, "", err
		}
		terminated, err := cvGetOr(state, "terminated")
		if err != nil {
			return nil, nil, "", err
		}
		tr, err := cvGet(terminated, "reason")
		if err != nil {
			return nil, nil, "", err
		}
		if pyjson.Equal(tr, "OOMKilled") || pyjson.Equal(tr, "Error") {
			return tr, restarts, ready, nil
		}
		lr, err := cvGet(last, "reason")
		if err != nil {
			return nil, nil, "", err
		}
		if pyjson.Equal(lr, "OOMKilled") && pyjson.Truthy(restarts) {
			return "OOMKilled", restarts, ready, nil
		}
	}
	phase, err := cvGet(status, "phase")
	if err != nil {
		return nil, nil, "", err
	}
	if pyjson.Equal(phase, "Pending") {
		condsV, err := cvGet(status, "conditions")
		if err != nil {
			return nil, nil, "", err
		}
		conds, err := cvIter(condsV)
		if err != nil {
			return nil, nil, "", err
		}
		for _, cond := range conds {
			t, err := cvGet(cond, "type")
			if err != nil {
				return nil, nil, "", err
			}
			if !pyjson.Equal(t, "PodScheduled") {
				continue
			}
			s, err := cvGet(cond, "status")
			if err != nil {
				return nil, nil, "", err
			}
			if pyjson.Equal(s, "False") {
				return "Unschedulable", restarts, ready, nil
			}
		}
	}
	if !pyjson.Truthy(phase) {
		phase = "Unknown"
	}
	return phase, restarts, ready, nil
}

// cvLimits is _limits: the sum of the containers' limits, None where none has one.
func cvLimits(pod any) (any, any, error) {
	spec, err := cvGetOr(pod, "spec")
	if err != nil {
		return nil, nil, err
	}
	cs, err := cvGet(spec, "containers")
	if err != nil {
		return nil, nil, err
	}
	containers, err := cvIter(cs)
	if err != nil {
		return nil, nil, err
	}
	cpu, anyCPU, anyMemory := 0.0, false, false
	var memory any = int64(0)
	for _, c := range containers {
		res, err := cvGetOr(c, "resources")
		if err != nil {
			return nil, nil, err
		}
		lim, err := cvGetOr(res, "limits")
		if err != nil {
			return nil, nil, err
		}
		q, err := cvGet(lim, "cpu")
		if err != nil {
			return nil, nil, err
		}
		if m, ok := cpuMillis(q); ok {
			cpu += m
			anyCPU = true
		}
		q, err = cvGet(lim, "memory")
		if err != nil {
			return nil, nil, err
		}
		b, err := memoryBytes(q)
		if err != nil {
			return nil, nil, err
		}
		if b != nil {
			memory = cvAddInt(memory, b)
			anyMemory = true
		}
	}
	var outCPU, outMemory any
	if anyCPU {
		outCPU = cpu
	}
	if anyMemory {
		outMemory = memory
	}
	return outCPU, outMemory, nil
}

// cvLabels is (p.get("metadata") or {}).get("labels") or {}.
func cvLabels(pod any) (any, error) {
	meta, err := cvGetOr(pod, "metadata")
	if err != nil {
		return nil, err
	}
	return cvGetOr(meta, "labels")
}

// cvSortKey is a value sorted() compares: texts with texts, numbers with numbers.
type cvSortKey struct {
	text  string
	num   float64
	isNum bool
}

func cvSortable(v any) (cvSortKey, bool) {
	if s, ok := v.(string); ok {
		return cvSortKey{text: s}, true
	}
	if f, ok := cvNumber(v); ok {
		return cvSortKey{num: f, isNum: true}, true
	}
	return cvSortKey{}, false
}

// cvSort sorts the items by their keys as sorted() would, stably; an error where Python
// cannot compare two of them.
func cvSort(items []any, keys []any, reverse bool) error {
	if len(items) < 2 {
		return nil
	}
	ks := make([]cvSortKey, len(keys))
	for i, k := range keys {
		s, ok := cvSortable(k)
		if !ok {
			return fmt.Errorf("'<' not supported between instances of '%s' and '%s'", cvType(k), cvType(k))
		}
		ks[i] = s
	}
	for _, k := range ks[1:] {
		if k.isNum != ks[0].isNum {
			return errors.New("'<' not supported between instances of 'str' and 'int'")
		}
	}
	idx := make([]int, len(items))
	for i := range idx {
		idx[i] = i
	}
	less := func(a, b cvSortKey) bool {
		if a.isNum {
			return a.num < b.num
		}
		return a.text < b.text
	}
	sort.SliceStable(idx, func(i, j int) bool {
		if reverse {
			return less(ks[idx[j]], ks[idx[i]])
		}
		return less(ks[idx[i]], ks[idx[j]])
	})
	sorted := make([]any, len(items))
	for i, j := range idx {
		sorted[i] = items[j]
	}
	copy(items, sorted)
	return nil
}

// cvWho is _who: what started the runs of the Jobs, and the environments' names.
func cvWho(ctx context.Context, d *Deps, runIDs, envIDs []int64) (map[int64]pyjson.Object, map[int64]string, error) {
	runs := map[int64]pyjson.Object{}
	if len(runIDs) > 0 {
		type run struct {
			id                          int64
			flow, script, group, parent *int64
		}
		read := func(ids []int64) (map[int64]run, []run, error) {
			rows, err := d.DB.Query(ctx, `SELECT id, flow_id, saved_script_id, flow_group_id, parent_run_id FROM runs WHERE id = ANY($1)`, ids)
			if err != nil {
				return nil, nil, err
			}
			defer rows.Close()
			byID, list := map[int64]run{}, []run{}
			for rows.Next() {
				var r run
				if err := rows.Scan(&r.id, &r.flow, &r.script, &r.group, &r.parent); err != nil {
					return nil, nil, err
				}
				byID[r.id] = r
				list = append(list, r)
			}
			return byID, list, rows.Err()
		}
		_, list, err := read(runIDs)
		if err != nil {
			return nil, nil, err
		}
		var parents []int64
		for _, r := range list {
			if r.parent != nil && *r.parent != 0 {
				parents = append(parents, *r.parent)
			}
		}
		heads := map[int64]run{}
		if len(parents) > 0 {
			if heads, _, err = read(parents); err != nil {
				return nil, nil, err
			}
		}
		named := func(table string, id int64) (string, bool, error) {
			var name string
			err := d.DB.QueryRow(ctx, `SELECT name FROM `+table+` WHERE id = $1`, id).Scan(&name)
			if errors.Is(err, pgx.ErrNoRows) {
				return "", false, nil
			}
			return name, err == nil, err
		}
		orID := func(a *int64, b *int64) int64 {
			if a != nil && *a != 0 {
				return *a
			}
			if b != nil {
				return *b
			}
			return 0
		}
		for _, r := range list {
			var head *run
			if r.parent != nil && *r.parent != 0 {
				if h, ok := heads[*r.parent]; ok {
					head = &h
				}
			}
			var flowID, scriptID, groupID int64
			if head != nil {
				flowID, scriptID = orID(r.flow, head.flow), orID(r.script, head.script)
				groupID = orID(head.group, nil)
			} else {
				flowID, scriptID = orID(r.flow, nil), orID(r.script, nil)
			}
			for _, c := range []struct {
				id          int64
				table, kind string
			}{{scriptID, "saved_scripts", "script"}, {flowID, "flows", "flow"}, {groupID, "flow_groups", "group"}} {
				if c.id == 0 {
					continue
				}
				name, ok, err := named(c.table, c.id)
				if err != nil {
					return nil, nil, err
				}
				if ok {
					runs[r.id] = pyjson.Object{{Key: "kind", Value: c.kind}, {Key: "id", Value: c.id}, {Key: "name", Value: name}}
					break
				}
			}
		}
	}
	envs := map[int64]string{}
	if len(envIDs) > 0 {
		rows, err := d.DB.Query(ctx, `SELECT id, name FROM script_environments WHERE id = ANY($1)`, envIDs)
		if err != nil {
			return nil, nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var id int64
			var name string
			if err := rows.Scan(&id, &name); err != nil {
				return nil, nil, err
			}
			envs[id] = name
		}
		if err := rows.Err(); err != nil {
			return nil, nil, err
		}
	}
	return runs, envs, nil
}

// cvID is an id read from a label, for the database: false past what a column holds.
func cvID(v any) (int64, bool) {
	n, ok := v.(int64)
	return n, ok
}

// clusterSnapshot is snapshot: the page's content, read now.
func clusterSnapshot(ctx context.Context, d *Deps) (pyjson.Object, error) {
	spaces := clusterNamespaces(d)
	results := make([]cvNamespaceResult, len(spaces))
	errs := make([]error, len(spaces))
	var wg sync.WaitGroup
	for i, ns := range spaces {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results[i], errs[i] = cvNamespace(ctx, ns.name)
		}()
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			return nil, err
		}
	}
	metricsSeen := false
	runSet, envSet := map[int64]bool{}, map[int64]bool{}
	var runIDs, envIDs []int64
	for _, res := range results {
		metricsSeen = metricsSeen || res.hasUsage
		for _, p := range res.pods {
			labels, err := cvLabels(p)
			if err != nil {
				return nil, err
			}
			for _, key := range []string{cvLabelScriptRun, cvLabelContainerRun, cvLabelEnv} {
				v, err := cvGet(labels, key)
				if err != nil {
					return nil, err
				}
				n, err := cvInt(v)
				if err != nil {
					return nil, err
				}
				id, ok := cvID(n)
				if n == nil || !ok {
					continue
				}
				if key == cvLabelEnv {
					if !envSet[id] {
						envSet[id] = true
						envIDs = append(envIDs, id)
					}
				} else if !runSet[id] {
					runSet[id] = true
					runIDs = append(runIDs, id)
				}
			}
		}
	}
	startedBy, envNames, err := cvWho(ctx, d, runIDs, envIDs)
	if err != nil {
		return nil, err
	}
	outSpaces, events := []any{}, []any{}
	now := time.Now().UTC()
	for i, ns := range spaces {
		res := results[i]
		pods := append([]any(nil), res.pods...)
		keys := make([]any, len(pods))
		for j, p := range pods {
			meta, err := cvGetOr(p, "metadata")
			if err != nil {
				return nil, err
			}
			name, err := cvGet(meta, "name")
			if err != nil {
				return nil, err
			}
			if !pyjson.Truthy(name) {
				name = ""
			}
			keys[j] = name
		}
		if err := cvSort(pods, keys, false); err != nil {
			return nil, err
		}
		items := []any{}
		for _, p := range pods {
			meta, err := cvGetOr(p, "metadata")
			if err != nil {
				return nil, err
			}
			spec, err := cvGetOr(p, "spec")
			if err != nil {
				return nil, err
			}
			status, err := cvGetOr(p, "status")
			if err != nil {
				return nil, err
			}
			labels, err := cvGetOr(meta, "labels")
			if err != nil {
				return nil, err
			}
			state, restarts, ready, err := cvState(p)
			if err != nil {
				return nil, err
			}
			cpuLimit, memoryLimit, err := cvLimits(p)
			if err != nil {
				return nil, err
			}
			name, err := cvGet(meta, "name")
			if err != nil {
				return nil, err
			}
			var cpu, memory any
			if res.hasUsage {
				key, ok := cvKey(name)
				if !ok {
					return nil, fmt.Errorf("unhashable type: '%s'", cvType(name))
				}
				if u, has := res.usage[key]; has {
					cpu, memory = u.cpu, u.memory
				}
			} else if _, ok := cvKey(name); !ok {
				return nil, fmt.Errorf("unhashable type: '%s'", cvType(name))
			}
			label := func(k string) (any, error) {
				v, err := cvGet(labels, k)
				if err != nil {
					return nil, err
				}
				return cvInt(v)
			}
			runID, err := label(cvLabelScriptRun)
			if err != nil {
				return nil, err
			}
			if !pyjson.Truthy(runID) {
				if runID, err = label(cvLabelContainerRun); err != nil {
					return nil, err
				}
			}
			envID, err := label(cvLabelEnv)
			if err != nil {
				return nil, err
			}
			var component any = ""
			for _, k := range []string{"app.kubernetes.io/component", "app", "tabularia.io/stage"} {
				v, err := cvGet(labels, k)
				if err != nil {
					return nil, err
				}
				if pyjson.Truthy(v) {
					component = v
					break
				}
			}
			phase, _ := cvGet(status, "phase")
			startTime, _ := cvGet(status, "startTime")
			node, err := cvGet(spec, "nodeName")
			if err != nil {
				return nil, err
			}
			var by, env any
			if pyjson.Truthy(runID) {
				if id, ok := cvID(runID); ok {
					if o, has := startedBy[id]; has {
						by = o
					}
				}
			}
			if pyjson.Truthy(envID) {
				if id, ok := cvID(envID); ok {
					if n, has := envNames[id]; has {
						env = n
					}
				}
			}
			items = append(items, pyjson.Object{
				{Key: "name", Value: name}, {Key: "component", Value: component}, {Key: "state", Value: state}, {Key: "phase", Value: phase},
				{Key: "ready", Value: ready}, {Key: "restarts", Value: restarts}, {Key: "started_at", Value: startTime}, {Key: "node", Value: node},
				{Key: "cpu_millis", Value: cpu}, {Key: "memory_bytes", Value: memory}, {Key: "cpu_limit_millis", Value: cpuLimit},
				{Key: "memory_limit_bytes", Value: memoryLimit}, {Key: "run_id", Value: runID}, {Key: "started_by", Value: by},
				{Key: "environment", Value: env},
			})
		}
		var errOut any
		if res.err != "" {
			errOut = res.err
		}
		outSpaces = append(outSpaces, pyjson.Object{{Key: "name", Value: ns.name}, {Key: "role", Value: ns.role}, {Key: "pods", Value: items},
			{Key: "error", Value: errOut}})
		for _, e := range res.events {
			obj, err := cvGetOr(e, "involvedObject")
			if err != nil {
				return nil, err
			}
			kind, err := cvGet(obj, "kind")
			if err != nil {
				return nil, err
			}
			kindText := ""
			if pyjson.Truthy(kind) {
				s, ok := kind.(string)
				if !ok {
					return nil, fmt.Errorf("'%s' object has no attribute 'lower'", cvType(kind))
				}
				kindText = pyd.Lower(s)
			}
			objName, _ := cvGet(obj, "name")
			nameText := ""
			if pyjson.Truthy(objName) {
				nameText = pyjson.Str(objName)
			}
			reason, err := cvGet(e, "reason")
			if err != nil {
				return nil, err
			}
			if !pyjson.Truthy(reason) {
				reason = ""
			}
			msgV, _ := cvGet(e, "message")
			var message any = ""
			if pyjson.Truthy(msgV) {
				switch m := msgV.(type) {
				case string:
					if r := []rune(m); len(r) > 500 {
						m = string(r[:500])
					}
					message = m
				case []any:
					if len(m) > 500 {
						m = m[:500]
					}
					message = m
				default:
					return nil, fmt.Errorf("'%s' object is not subscriptable", cvType(msgV))
				}
			}
			count, _ := cvGet(e, "count")
			if !pyjson.Truthy(count) {
				count = int64(1)
			}
			lastSeen, _ := cvGet(e, "lastTimestamp")
			if !pyjson.Truthy(lastSeen) {
				lastSeen, _ = cvGet(e, "eventTime")
			}
			if !pyjson.Truthy(lastSeen) {
				em, err := cvGetOr(e, "metadata")
				if err != nil {
					return nil, err
				}
				if lastSeen, err = cvGet(em, "creationTimestamp"); err != nil {
					return nil, err
				}
			}
			events = append(events, pyjson.Object{{Key: "namespace", Value: ns.name}, {Key: "object", Value: kindText + "/" + nameText},
				{Key: "reason", Value: reason}, {Key: "message", Value: message}, {Key: "count", Value: count}, {Key: "last_seen", Value: lastSeen}})
		}
	}
	keys := make([]any, len(events))
	for i, e := range events {
		v := e.(pyjson.Object).Value("last_seen")
		if !pyjson.Truthy(v) {
			v = ""
		}
		keys[i] = v
	}
	if err := cvSort(events, keys, true); err != nil {
		return nil, err
	}
	if len(events) > cvMaxEvents {
		events = events[:cvMaxEvents]
	}
	return pyjson.Object{{Key: "generated_at", Value: pyjson.Isoformat(now)}, {Key: "metrics", Value: metricsSeen},
		{Key: "namespaces", Value: outSpaces}, {Key: "events", Value: events}}, nil
}
