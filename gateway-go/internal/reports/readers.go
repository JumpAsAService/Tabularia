// Package reports is the part of services/reports.py other modules read: the reports that
// read an object (a SQL script, a saved view…), and the flow groups that send them.
package reports

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Report is the part of a reports row read here.
type Report struct {
	ID        int64
	Name      string
	ProjectID int64
}

// PointsTo is _POINTS_TO: the object's kind → the source's kind and its key.
var PointsTo = map[string][2]string{"sql_script": {"sql_script", "script_id"}, "view": {"view", "view_id"},
	"datasource": {"datasource", "datasource_id"}, "connection": {"sql", "connection_id"}}

// Reading is reports_reading (and dashboards_reading, with its tables): the objects one of
// whose datasets, in the current version, names the object; in the database's order.
func Reading(ctx context.Context, q perm.Querier, table, versions, fk, kind string, objectID int64) ([]Report, error) {
	pt := PointsTo[kind]
	sourceKind, key := pt[0], pt[1]
	rows, err := q.Query(ctx, fmt.Sprintf(`SELECT o.id, o.name, o.project_id, v.content FROM %s o JOIN %s v
		ON o.id = v.%s AND o.current_version = v.version WHERE v.content LIKE '%%' || $1 || '%%'`, table, versions, fk),
		`"`+key+`"`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Report
	for rows.Next() {
		var r Report
		var content string
		if err := rows.Scan(&r.ID, &r.Name, &r.ProjectID, &content); err != nil {
			return nil, err
		}
		if names(content, sourceKind, key, objectID) {
			out = append(out, r)
		}
	}
	return out, rows.Err()
}

// names: a dataset of the definition whose source is {kind: sourceKind, key: objectID}. An
// unreadable definition is the empty one (_parsed), which has no datasets.
func names(content, sourceKind, key string, objectID int64) bool {
	v, err := pyjson.Loads(content)
	if err != nil {
		return false
	}
	def, ok := v.(pyjson.Object)
	if !ok {
		return false
	}
	ds, _ := def.Get("datasets")
	list, _ := ds.([]any)
	for _, d := range list {
		o, ok := d.(pyjson.Object)
		if !ok {
			continue
		}
		src, _ := o.Get("source")
		s, _ := src.(pyjson.Object)
		k, _ := s.Get("kind")
		id, _ := s.Get(key)
		if k == sourceKind && sameInt(id, objectID) {
			return true
		}
	}
	return false
}

// sameInt is Python's `==` of a JSON value and an int (1.0 == 1, True == 1).
func sameInt(v any, n int64) bool {
	switch x := v.(type) {
	case int64:
		return x == n
	case float64:
		return x == float64(n)
	case bool:
		return (x && n == 1) || (!x && n == 0)
	}
	return false
}

// CanSee is reports.can_see (and dashboards.can_see).
func CanSee(ctx context.Context, c *perm.Checker, projectID int64) (bool, error) {
	if admin, err := c.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	return c.Has(ctx, projectID, perm.View)
}

// NotReadBy is not_read_by_reports / not_read_by_dashboards: the 409 when some read the
// object about to be deleted, naming the ones this person sees and counting the others.
// `noun` is report or dashboard; `extra` the detail's keys before the list of names.
func NotReadBy(ctx context.Context, c *perm.Checker, found []Report, what, noun string, extra pyjson.Object,
	listKey string) (*auth.Refusal, any, error) {
	if len(found) == 0 {
		return nil, nil, nil
	}
	seen := []string{}
	for _, r := range found {
		ok, err := CanSee(ctx, c, r.ProjectID)
		if err != nil {
			return nil, nil, err
		}
		if ok {
			seen = append(seen, r.Name)
		}
	}
	sort.Strings(seen)
	others := len(found) - len(seen)
	quoted := make([]string, 0, 10)
	for i, n := range seen {
		if i == 10 {
			break
		}
		quoted = append(quoted, "«"+n+"»")
	}
	who := strings.Join(quoted, ", ")
	if len(seen) > 10 {
		who += " …"
	}
	switch {
	case len(seen) == 0:
		plural := noun + "s"
		if others == 1 {
			plural = noun
		}
		who = fmt.Sprintf("%d %s you cannot see", others, plural)
	case others > 0:
		who = fmt.Sprintf("the %ss %s and %d you cannot see", noun, who, others)
	default:
		who = fmt.Sprintf("the %ss %s", noun, who)
	}
	detail := pyjson.Object{{Key: "message", Value: fmt.Sprintf("%s is read by %s: change them first", what, who)}}
	detail = append(detail, extra...)
	detail = append(detail, pyjson.KV{Key: listKey, Value: seen})
	return &auth.Refusal{Status: http.StatusConflict}, detail, nil
}

// GroupsUsing is groups_using: the flow groups with a Pushmail cell that sends the report
// (`dashboards` false) or the dashboard.
func GroupsUsing(ctx context.Context, q perm.Querier, id int64, dashboards bool) ([]flowgroups.Group, error) {
	fragment := `"reportId"`
	if dashboards {
		fragment = `"dashboardId"`
	}
	groups, err := flowgroups.Containing(ctx, q, fragment)
	if err != nil {
		return nil, err
	}
	var out []flowgroups.Group
	for _, g := range groups {
		if sends(flowgroups.DefinitionOf(g.Definition), id, dashboards) {
			out = append(out, g)
		}
	}
	return out, nil
}

func sends(def flowgroups.Definition, id int64, dashboards bool) bool {
	for _, row := range def.Steps {
		for _, c := range row {
			if c == nil || c.Kind != "pushmail" || c.Pushmail == nil {
				continue
			}
			refs := c.Pushmail.Reports
			if dashboards {
				refs = c.Pushmail.Dashboards
			}
			for _, r := range refs {
				if r != nil && *r == id {
					return true
				}
			}
		}
	}
	return false
}

// ReportsReading is reports_reading.
func ReportsReading(ctx context.Context, q perm.Querier, kind string, objectID int64) ([]Report, error) {
	return Reading(ctx, q, "reports", "report_versions", "report_id", kind, objectID)
}

// NotReadByReports is not_read_by_reports: the refusal and its detail, or nil.
func NotReadByReports(ctx context.Context, c *perm.Checker, q perm.Querier, kind string, objectID int64, what string) (*auth.Refusal, any, error) {
	found, err := ReportsReading(ctx, q, kind, objectID)
	if err != nil {
		return nil, nil, err
	}
	extra := pyjson.Object{{Key: "flows", Value: []any{}}, {Key: "groups", Value: []any{}}}
	return NotReadBy(ctx, c, found, what, "report", extra, "reports")
}

// Reader is a report with a group that sends it.
type Reader struct {
	Report Report
	Group  flowgroups.Group
}

// GroupsReading is groups_reading.
func GroupsReading(ctx context.Context, q perm.Querier, kind string, objectID int64) ([]Reader, error) {
	found, err := ReportsReading(ctx, q, kind, objectID)
	if err != nil {
		return nil, err
	}
	var out []Reader
	for _, r := range found {
		groups, err := GroupsUsing(ctx, q, r.ID, false)
		if err != nil {
			return nil, err
		}
		for _, g := range groups {
			out = append(out, Reader{r, g})
		}
	}
	return out, nil
}
