package versioned

import (
	"context"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// FlowGroup is the `flow_groups` row, the columns a report's change reads and writes.
type FlowGroup struct {
	ID             int64
	Name           string
	Definition     string
	RunSchedule    *string
	RunScheduledBy *int64
	NextRunAt      *time.Time
}

// Sent is a report or a dashboard of a Pushmail cell (schemas/models.py PushmailReportCell,
// PushmailDashboardCell): its id and its parameters' values.
type Sent struct {
	ID     *int64
	Params pyjson.Object
}

// Pushmail names where a kind of object sits in a Pushmail cell.
type Pushmail struct {
	List  string // "reports"
	IDKey string // "reportId"
}

// sentBy is flow_groups.definition_of(group).steps, what the Pushmail cells send of a kind.
// The definition is the one the flow groups' routes validated (FlowGroupDefinition) when they
// wrote it; one that does not read as a grid is an empty one, as definition_of says.
func (pm Pushmail) sentBy(definition string) [][]Sent {
	if definition == "" {
		definition = "{}"
	}
	v, err := pyjson.Loads(definition)
	if err != nil {
		return nil
	}
	root, ok := v.(pyjson.Object)
	if !ok {
		return nil
	}
	stepsV, _ := root.Get("steps")
	if stepsV == nil {
		return nil
	}
	steps, ok := stepsV.([]any)
	if !ok || len(steps) > 50 { // MAX_GROUP_STEPS
		return nil
	}
	var cells [][]Sent
	for _, rowV := range steps {
		row, ok := rowV.([]any)
		if !ok || len(row) > 20 { // MAX_GROUP_COLUMNS
			return nil
		}
		for _, cellV := range row {
			if cellV == nil {
				continue
			}
			cell, ok := cellV.(pyjson.Object)
			if !ok {
				return nil
			}
			kind, _ := cell.Get("kind")
			cellPM, _ := cell.Get("pushmail")
			if kind != "pushmail" || cellPM == nil {
				continue
			}
			pmo, ok := cellPM.(pyjson.Object)
			if !ok {
				return nil
			}
			var out []Sent
			rs, _ := pmo.Get(pm.List)
			list, _ := rs.([]any)
			for _, rv := range list {
				ro, ok := rv.(pyjson.Object)
				if !ok {
					return nil
				}
				var pr Sent
				if id, ok := ro.Get(pm.IDKey); ok {
					if n, ok := id.(int64); ok {
						pr.ID = &n
					}
				}
				if ps, ok := ro.Get("params"); ok {
					pr.Params, _ = ps.(pyjson.Object)
				}
				out = append(out, pr)
			}
			cells = append(cells, out)
		}
	}
	return cells
}

func (pm Pushmail) sends(definition string, id int64) bool {
	for _, cell := range pm.sentBy(definition) {
		for _, r := range cell {
			if r.ID != nil && *r.ID == id {
				return true
			}
		}
	}
	return false
}

// GroupsUsing is groups_using: the flow groups with a Pushmail cell that sends the object.
func (pm Pushmail) GroupsUsing(ctx context.Context, q perm.Querier, id int64) ([]*FlowGroup, error) {
	rows, err := q.Query(ctx, `SELECT id, name, definition, run_schedule, run_scheduled_by, next_run_at FROM flow_groups
		WHERE definition LIKE '%' || $1 || '%'`, `"`+pm.IDKey+`"`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*FlowGroup
	for rows.Next() {
		var g FlowGroup
		if err := rows.Scan(&g.ID, &g.Name, &g.Definition, &g.RunSchedule, &g.RunScheduledBy, &g.NextRunAt); err != nil {
			return nil, err
		}
		if pm.sends(g.Definition, id) {
			out = append(out, &g)
		}
	}
	return out, rows.Err()
}

// GivenBy is given_by: the parameters every Pushmail cell of the group that sends the object
// gives a value (an empty text or list is none).
func (pm Pushmail) GivenBy(g *FlowGroup, id int64) map[string]bool {
	var given map[string]bool
	for _, cell := range pm.sentBy(g.Definition) {
		for _, r := range cell {
			if r.ID == nil || *r.ID != id {
				continue
			}
			names := map[string]bool{}
			for _, kv := range r.Params {
				if !empty(kv.Value) {
					names[kv.Key] = true
				}
			}
			if given == nil {
				given = names
			} else {
				for k := range given {
					if !names[k] {
						delete(given, k)
					}
				}
			}
		}
	}
	if given == nil {
		return map[string]bool{}
	}
	return given
}

// empty is `v in (None, "", [])`.
func empty(v any) bool {
	switch x := v.(type) {
	case nil:
		return true
	case string:
		return x == ""
	case []any:
		return len(x) == 0
	}
	return false
}
