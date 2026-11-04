// Package flowgroups is the part of services/flow_groups.py other modules read: a group's
// grid (`definition_of`), and its schedule switched off when something it runs changes.
package flowgroups

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pydconv"
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

// Model is FlowGroupDefinition.model_validate(v): the grid validated as pydantic does (pyd's
// generated model), nil with the errors when it does not pass.
func Model(v any) (*pyd.ModelValue, []*pyd.Error) {
	out, errs := gatewaymodels.FlowGroupDefinition.Type().Validate(v)
	if len(errs) > 0 {
		return nil, errs
	}
	return out.(*pyd.ModelValue), nil
}

// Parse is definition_of: json.loads of the stored text, then the model; an unreadable grid
// reads as an empty one.
func Parse(definition string) *pyd.ModelValue {
	if definition == "" {
		definition = "{}"
	}
	if v, err := pyjson.Loads(definition); err == nil {
		if mv, errs := Model(pydconv.ToPyd(v)); errs == nil {
			return mv
		}
	}
	mv, _ := Model(pyd.NewObject())
	return mv
}

// DefinitionOf is definition_of, as the fields other modules read: a cell's kind and id, a
// Pushmail cell's reports and dashboards.
func DefinitionOf(definition string) Definition {
	return Read(Parse(definition))
}

// Read gives the fields of a validated grid.
func Read(mv *pyd.ModelValue) Definition {
	out := Definition{Steps: [][]*Cell{}}
	steps, _ := mv.Get("steps").([]any)
	for _, row := range steps {
		cells, _ := row.([]any)
		r := make([]*Cell, 0, len(cells))
		for _, c := range cells {
			cm, ok := c.(*pyd.ModelValue)
			if !ok {
				r = append(r, nil)
				continue
			}
			cell := &Cell{Kind: cm.Get("kind").(string)}
			cell.ID, _ = cm.Get("id").(int64)
			if pm, ok := cm.Get("pushmail").(*pyd.ModelValue); ok {
				cell.Pushmail = &Pushmail{Reports: refs(pm.Get("reports"), "reportId"), Dashboards: refs(pm.Get("dashboards"), "dashboardId")}
			}
			r = append(r, cell)
		}
		out.Steps = append(out.Steps, r)
	}
	return out
}

func refs(v any, key string) []*int64 {
	list, _ := v.([]any)
	var out []*int64
	for _, e := range list {
		var ref *int64
		if m, ok := e.(*pyd.ModelValue); ok {
			if n, ok := m.Get(key).(int64); ok {
				ref = &n
			}
		}
		out = append(out, ref)
	}
	return out
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
