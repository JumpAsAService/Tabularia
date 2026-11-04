// Package flowgroups is the part of services/flow_groups.py other modules read: a group's
// grid (`definition_of`), and its schedule switched off when something it runs changes.
package flowgroups

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Group is a row of flow_groups.
type Group struct {
	ID             int64
	Name           string
	ProjectID      int64
	Definition     string
	RunSchedule    *string
	RunScheduledBy *int64
	NextRunAt      *time.Time
}

// Cell is a FlowGroupCell, the fields read here.
type Cell struct {
	Kind     string
	ID       int64
	Pushmail *Pushmail
}

// Pushmail is a PushmailCell: the reports and dashboards it sends.
type Pushmail struct {
	Reports    []*int64 // reportId of each report
	Dashboards []*int64 // dashboardId of each dashboard
}

// Definition is FlowGroupDefinition: rows are steps, a nil cell keeps its column.
type Definition struct {
	Steps [][]*Cell
}

var kinds = map[string]bool{"flow": true, "refresh": true, "script": true, "container": true, "pushmail": true, "sql": true}

// DefinitionOf is definition_of: the group's grid; an unreadable one reads as empty. The
// grid is read for the fields the gateway looks at (a cell's kind and id, the Pushmail
// cell's reports and dashboards): the definitions the gateway stores passed the whole
// validation when they were saved.
func DefinitionOf(definition string) Definition {
	if definition == "" {
		definition = "{}"
	}
	v, err := pyjson.Loads(definition)
	if err != nil {
		return Definition{}
	}
	obj, ok := v.(pyjson.Object)
	if !ok {
		return Definition{}
	}
	raw, _ := obj.Get("steps")
	if raw == nil {
		return Definition{}
	}
	steps, ok := raw.([]any)
	if !ok {
		return Definition{}
	}
	out := Definition{Steps: make([][]*Cell, 0, len(steps))}
	for _, s := range steps {
		row, ok := s.([]any)
		if !ok {
			return Definition{}
		}
		cells := make([]*Cell, 0, len(row))
		for _, c := range row {
			if c == nil {
				cells = append(cells, nil)
				continue
			}
			cell, ok := readCell(c)
			if !ok {
				return Definition{}
			}
			cells = append(cells, cell)
		}
		out.Steps = append(out.Steps, cells)
	}
	return out
}

func readCell(v any) (*Cell, bool) {
	obj, ok := v.(pyjson.Object)
	if !ok {
		return nil, false
	}
	k, _ := obj.Get("kind")
	kind, ok := k.(string)
	if !ok || !kinds[kind] {
		return nil, false
	}
	i, _ := obj.Get("id")
	id, ok := i.(int64)
	if !ok {
		return nil, false
	}
	cell := &Cell{Kind: kind, ID: id}
	if p, _ := obj.Get("pushmail"); p != nil {
		pm, ok := p.(pyjson.Object)
		if !ok {
			return nil, false
		}
		cell.Pushmail = &Pushmail{}
		for _, part := range []struct {
			list, key string
			into      *[]*int64
		}{{"reports", "reportId", &cell.Pushmail.Reports}, {"dashboards", "dashboardId", &cell.Pushmail.Dashboards}} {
			raw, _ := pm.Get(part.list)
			if raw == nil {
				continue
			}
			items, ok := raw.([]any)
			if !ok {
				return nil, false
			}
			for _, it := range items {
				o, ok := it.(pyjson.Object)
				if !ok {
					return nil, false
				}
				var ref *int64
				if r, _ := o.Get(part.key); r != nil {
					n, ok := r.(int64)
					if !ok {
						return nil, false
					}
					ref = &n
				}
				*part.into = append(*part.into, ref)
			}
		}
	}
	return cell, true
}

// Containing are the groups whose definition holds `fragment` (FlowGroup.definition.contains),
// in the database's order.
func Containing(ctx context.Context, q perm.Querier, fragment string) ([]Group, error) {
	rows, err := q.Query(ctx, `SELECT id, name, project_id, definition, run_schedule, run_scheduled_by, next_run_at
		FROM flow_groups WHERE definition LIKE '%' || $1 || '%'`, fragment)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Group
	for rows.Next() {
		var g Group
		if err := rows.Scan(&g.ID, &g.Name, &g.ProjectID, &g.Definition, &g.RunSchedule, &g.RunScheduledBy, &g.NextRunAt); err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

// Off is a group whose schedule a change switched off: what record_groups_off writes.
type Off struct {
	ID     int64
	Name   string
	Detail pyjson.Object
}

// Affected is a group with the reason its schedule would go.
type Affected struct {
	Group  Group
	Reason string
}

// TurnOff is reports.turn_off_groups: the schedules set by someone other than `userID`,
// switched off in `tx`; a group listed twice is switched off once (the second time its
// schedule is gone already).
func TurnOff(ctx context.Context, tx pgx.Tx, userID int64, affected []Affected) ([]Off, error) {
	var off []Off
	done := map[int64]bool{}
	for _, a := range affected {
		g := a.Group
		if done[g.ID] || g.RunSchedule == nil || *g.RunSchedule == "" || g.RunScheduledBy == nil || *g.RunScheduledBy == userID {
			continue
		}
		done[g.ID] = true
		detail := pyjson.Object{{Key: "turned_off", Value: true}, {Key: "author", Value: *g.RunScheduledBy},
			{Key: "reason", Value: a.Reason}}
		if _, err := tx.Exec(ctx, `UPDATE flow_groups SET run_schedule = NULL, run_scheduled_by = NULL, next_run_at = NULL
			WHERE id = $1`, g.ID); err != nil {
			return nil, err
		}
		off = append(off, Off{ID: g.ID, Name: g.Name, Detail: detail})
	}
	return off, nil
}
