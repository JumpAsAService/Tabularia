// Package dashboards is the part of services/dashboards.py other modules read: the
// dashboards that read an object, and the flow groups that send them.
package dashboards

import (
	"context"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
)

// Reading is dashboards_reading.
func Reading(ctx context.Context, q perm.Querier, kind string, objectID int64) ([]reports.Report, error) {
	return reports.Reading(ctx, q, "dashboards", "dashboard_versions", "dashboard_id", kind, objectID)
}

// NotReadBy is not_read_by_dashboards: the refusal and its detail, or nil.
func NotReadBy(ctx context.Context, c *perm.Checker, q perm.Querier, kind string, objectID int64, what string) (*auth.Refusal, any, error) {
	found, err := Reading(ctx, q, kind, objectID)
	if err != nil {
		return nil, nil, err
	}
	extra := pyjson.Object{{Key: "flows", Value: []any{}}, {Key: "groups", Value: []any{}}, {Key: "reports", Value: []any{}}}
	return reports.NotReadBy(ctx, c, found, what, "dashboard", extra, "dashboards")
}

// Reader is a dashboard with a group that sends it.
type Reader struct {
	Dashboard reports.Report
	Group     flowgroups.Group
}

// GroupsReading is groups_reading: (dashboard, group) for every dashboard reading the
// object and every group sending it.
func GroupsReading(ctx context.Context, q perm.Querier, kind string, objectID int64) ([]Reader, error) {
	found, err := Reading(ctx, q, kind, objectID)
	if err != nil {
		return nil, err
	}
	var out []Reader
	for _, d := range found {
		groups, err := reports.GroupsUsing(ctx, q, d.ID, true)
		if err != nil {
			return nil, err
		}
		for _, g := range groups {
			out = append(out, Reader{d, g})
		}
	}
	return out, nil
}
