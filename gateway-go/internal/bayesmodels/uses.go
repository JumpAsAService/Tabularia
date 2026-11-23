package bayesmodels

import (
	"context"
	"errors"
	"sort"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// The keys a flow node writes in its data to pin a model (flow-node's interface, fixed by the spec:
// a node of type NodeType).
const (
	NodeType   = "bayes_model"
	KeyModel   = "bayesModelId"
	KeyVersion = "bayesModelVersion"
)

// Use is a flow node that pins a model: the flow, its folder (whether the reader may open it is
// the caller's), the node and the version it pins (0 when the node names none).
type Use struct {
	FlowID        int64
	FlowName      string
	FlowProjectID int64
	NodeID        string
	Version       int64
}

// Pin is a node of a flow definition that names a model, with the version it pins (0: none).
type Pin struct {
	NodeID  string
	ModelID int64
	Version int64
}

// Pinned is what a flow definition's nodes pin: every node whose data names a model by an integer
// id (whatever its type: a node that names a model keeps it from being deleted).
func Pinned(definition string) []Pin {
	if definition == "" {
		return nil
	}
	v, err := pyjson.Loads(definition)
	if err != nil {
		return nil
	}
	doc, ok := v.(pyjson.Object)
	if !ok {
		return nil
	}
	nodes, _ := field(doc, "nodes").([]any)
	var out []Pin
	for _, n := range nodes {
		o, ok := n.(pyjson.Object)
		if !ok {
			continue
		}
		data, _ := field(o, "data").(pyjson.Object)
		id, ok := field(data, KeyModel).(int64)
		if !ok {
			continue
		}
		node, _ := field(o, "id").(string)
		version, _ := field(data, KeyVersion).(int64)
		out = append(out, Pin{NodeID: node, ModelID: id, Version: version})
	}
	return out
}

func field(o pyjson.Object, k string) any {
	v, _ := o.Get(k)
	return v
}

// Using lists the flow nodes that pin the model, by flow id then node id. The flows are read
// whose definition holds the key (the LIKE prefilter of savedscripts.Using), then each node.
func Using(ctx context.Context, q perm.Querier, modelID int64) ([]Use, error) {
	rows, err := q.Query(ctx, `SELECT id, name, project_id, definition FROM flows
		WHERE definition LIKE '%' || '"`+KeyModel+`"' || '%' ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Use{}
	for rows.Next() {
		var id, project int64
		var name, def string
		if err := rows.Scan(&id, &name, &project, &def); err != nil {
			return nil, err
		}
		for _, u := range Pinned(def) {
			if u.ModelID == modelID {
				out = append(out, Use{FlowID: id, FlowName: name, FlowProjectID: project, NodeID: u.NodeID, Version: u.Version})
			}
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].FlowID != out[j].FlowID {
			return out[i].FlowID < out[j].FlowID
		}
		return out[i].NodeID < out[j].NodeID
	})
	return out, nil
}

// UseCounts is how many flows pin each model, in one read of the flows that hold the key (a flow
// with two nodes of one model counts once, as in the detail's `uses`). Models no flow pins are absent.
func UseCounts(ctx context.Context, q perm.Querier) (map[int64]int, error) {
	rows, err := q.Query(ctx, `SELECT definition FROM flows WHERE definition LIKE '%' || '"`+KeyModel+`"' || '%'`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]int{}
	for rows.Next() {
		var def string
		if err := rows.Scan(&def); err != nil {
			return nil, err
		}
		in := map[int64]bool{}
		for _, u := range Pinned(def) {
			in[u.ModelID] = true
		}
		for id := range in {
			out[id]++
		}
	}
	return out, rows.Err()
}

// References is the names of the flows that pin the model, each once, sorted.
func References(ctx context.Context, q perm.Querier, modelID int64) ([]string, error) {
	uses, err := Using(ctx, q, modelID)
	if err != nil {
		return nil, err
	}
	seen := map[int64]bool{}
	out := []string{}
	for _, u := range uses {
		if !seen[u.FlowID] {
			seen[u.FlowID] = true
			out = append(out, u.FlowName)
		}
	}
	sort.Strings(out)
	return out, nil
}

// Newness is what the notice «a newer version exists» needs for a pinned version.
type Newness struct {
	Current int64
	// Newer: a version after the pinned one exists.
	Newer bool
	// Changed: the current version's semantic hash differs from the pinned one's (a version that
	// only moved boxes is newer but not changed: the notice says nothing then). True when the
	// pinned version is not there.
	Changed bool
	// PinnedMissing: the model has no such version.
	PinnedMissing bool
}

// Newer compares a pinned version with the model's current one; nil when the model is not there.
func Newer(ctx context.Context, q perm.Querier, modelID, pinned int64) (*Newness, error) {
	var current int64
	var currentHash string
	var pinnedHash *string
	err := q.QueryRow(ctx, `SELECT m.current_version, cv.semantic_hash, pv.semantic_hash FROM bayes_models m
		JOIN bayes_model_versions cv ON cv.model_id = m.id AND cv.version = m.current_version
		LEFT JOIN bayes_model_versions pv ON pv.model_id = m.id AND pv.version = $2
		WHERE m.id = $1`, modelID, pinned).Scan(&current, &currentHash, &pinnedHash)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	n := &Newness{Current: current, Newer: current > pinned}
	if pinnedHash == nil {
		n.PinnedMissing, n.Changed = true, true
	} else {
		n.Changed = *pinnedHash != currentHash
	}
	return n, nil
}
