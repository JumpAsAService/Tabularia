package dataapis

// What the page says of a data API's pods (migration 0041): the reconcile loop writes what the
// cluster says (data_api_status, one row per data API; the `cluster` side of data_api_pods),
// every pod writes its own report with its poll (the `report` side). And the usage: kept
// DATA_API__USAGE_RETENTION_DAYS, drawn by the page.

import (
	"context"
	"encoding/json"
	"errors"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// ClusterStatus is what the reconcile loop last saw of a data API's Deployment.
type ClusterStatus struct {
	// the pods wanted now (0 while paused; the HPA's count while autoscaling)
	Wanted int `json:"wanted"`
	// ready: answering; updated: of the latest definition of the pods
	Ready   int `json:"ready"`
	Updated int `json:"updated"`
	// the disk each pod has for its data files (a Kubernetes quantity)
	Disk string `json:"disk,omitempty"`
	// what the cluster refused or said is wrong ("" when nothing)
	Error string `json:"error,omitempty"`
}

// PodCluster is one pod as the cluster sees it.
type PodCluster struct {
	Name     string     `json:"name"`
	Phase    string     `json:"phase"`
	Ready    bool       `json:"ready"`
	Restarts int64      `json:"restarts"`
	Node     string     `json:"node,omitempty"`
	Started  *time.Time `json:"started,omitempty"`
	// why it does not run or is not ready, in the cluster's words (CrashLoopBackOff,
	// ImagePullBackOff, OOMKilled…), and what it said
	Reason  string `json:"reason,omitempty"`
	Message string `json:"message,omitempty"`
	// stopping (a rollout, a scale-in, a pause)
	Terminating bool `json:"terminating,omitempty"`
}

// PodRow is a row of data_api_pods: the cluster's side and the pod's own report.
type PodRow struct {
	Pod        string
	Cluster    *PodCluster
	ClusterAt  *time.Time
	Report     *dataapiwire.PodStatus
	ReportedAt *time.Time
}

// SetClusterStatus keeps what the loop saw of the data API.
func SetClusterStatus(ctx context.Context, q Querier, apiID int64, s ClusterStatus) error {
	_, err := q.Exec(ctx, `INSERT INTO data_api_status (data_api_id, cluster, checked_at) VALUES ($1, $2, $3)
		ON CONFLICT (data_api_id) DO UPDATE SET cluster = EXCLUDED.cluster, checked_at = EXCLUDED.checked_at`,
		apiID, dumps(s), Now())
	return err
}

// SetPodsCluster keeps the pods the cluster has of the data API now; the rows of the pods it
// no longer has go (with their reports).
func SetPodsCluster(ctx context.Context, q Querier, apiID int64, pods []PodCluster) error {
	names := make([]string, 0, len(pods))
	now := Now()
	for _, p := range pods {
		names = append(names, p.Name)
		if _, err := q.Exec(ctx, `INSERT INTO data_api_pods (data_api_id, pod, cluster, cluster_at, report) VALUES ($1, $2, $3, $4, '{}')
			ON CONFLICT (data_api_id, pod) DO UPDATE SET cluster = EXCLUDED.cluster, cluster_at = EXCLUDED.cluster_at`,
			apiID, p.Name, dumps(p), now); err != nil {
			return err
		}
	}
	_, err := q.Exec(ctx, `DELETE FROM data_api_pods WHERE data_api_id = $1 AND NOT (pod = ANY($2))`, apiID, names)
	return err
}

// a pod's name, as Kubernetes makes them
var podNameRe = regexp.MustCompile(`^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$`)

// MaxPodRows is the most pods a data API keeps rows of: a report from one more is left out
// (the pods of one data API are at most MaxReplicasPerAPI, and a rollout's new ones).
const MaxPodRows = 50

// RecordPodReport keeps a pod's report of itself; false when it is left out (not a pod's
// name, or too many pods already).
func RecordPodReport(ctx context.Context, q Querier, apiID int64, pod string, s dataapiwire.PodStatus) (bool, error) {
	if !podNameRe.MatchString(pod) {
		return false, nil
	}
	if len(s.Resources) > dataapiwire.MaxStatusResources {
		s.Resources = s.Resources[:dataapiwire.MaxStatusResources]
	}
	s.Error = cutText(s.Error)
	for i := range s.Resources {
		r := &s.Resources[i]
		r.Name, r.Version, r.Preparing, r.Error = cutText(r.Name), cutText(r.Version), cutText(r.Preparing), cutText(r.Error)
	}
	tag, err := q.Exec(ctx, `INSERT INTO data_api_pods (data_api_id, pod, cluster, report, reported_at)
		SELECT $1, $2, '{}', $3, $4 WHERE (SELECT count(*) FROM data_api_pods WHERE data_api_id = $1) < $5
		ON CONFLICT (data_api_id, pod) DO UPDATE SET report = EXCLUDED.report, reported_at = EXCLUDED.reported_at`,
		apiID, pod, dumps(s), Now(), MaxPodRows)
	if err != nil {
		return false, err
	}
	if tag.RowsAffected() == 0 { // the cap: an existing pod still updates its own row
		tag, err = q.Exec(ctx, `UPDATE data_api_pods SET report = $3, reported_at = $4 WHERE data_api_id = $1 AND pod = $2`,
			apiID, pod, dumps(s), Now())
		if err != nil {
			return false, err
		}
	}
	return tag.RowsAffected() == 1, nil
}

func cutText(s string) string {
	if r := []rune(s); len(r) > dataapiwire.MaxStatusText {
		return string(r[:dataapiwire.MaxStatusText]) + "…"
	}
	return s
}

// Status is what the page shows of the data API's pods: the loop's last look (nil when it has
// not looked yet) and the pods, by name.
func Status(ctx context.Context, q Querier, apiID int64) (*ClusterStatus, *time.Time, []PodRow, error) {
	var text string
	var at time.Time
	var cluster *ClusterStatus
	var checked *time.Time
	err := q.QueryRow(ctx, `SELECT cluster, checked_at FROM data_api_status WHERE data_api_id = $1`, apiID).Scan(&text, &at)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
	case err != nil:
		return nil, nil, nil, err
	default:
		cluster = &ClusterStatus{}
		_ = json.Unmarshal([]byte(text), cluster)
		checked = &at
	}
	rows, err := q.Query(ctx, `SELECT pod, cluster, cluster_at, report, reported_at FROM data_api_pods WHERE data_api_id = $1
		ORDER BY pod`, apiID)
	if err != nil {
		return nil, nil, nil, err
	}
	pods, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (PodRow, error) {
		var p PodRow
		var c, rep string
		if err := r.Scan(&p.Pod, &c, &p.ClusterAt, &rep, &p.ReportedAt); err != nil {
			return p, err
		}
		if p.ClusterAt != nil {
			p.Cluster = &PodCluster{}
			_ = json.Unmarshal([]byte(c), p.Cluster)
		}
		if p.ReportedAt != nil {
			p.Report = &dataapiwire.PodStatus{}
			_ = json.Unmarshal([]byte(rep), p.Report)
		}
		return p, nil
	})
	if pods == nil {
		pods = []PodRow{}
	}
	return cluster, checked, pods, err
}

// ForgetPods removes what is kept of a data API's pods: it has none in the cluster any more.
func ForgetPods(ctx context.Context, q Querier, apiID int64) error {
	_, err := q.Exec(ctx, `DELETE FROM data_api_pods WHERE data_api_id = $1`, apiID)
	return err
}

// DeleteUsageBefore removes the usage of the minutes before `t`: the number of rows removed.
func DeleteUsageBefore(ctx context.Context, q Querier, t time.Time) (int64, error) {
	tag, err := q.Exec(ctx, `DELETE FROM data_api_usage WHERE minute < $1`, t)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// UsagePoint is the usage of a data API in one bucket of time, every key together.
type UsagePoint struct {
	At        time.Time `json:"at"`
	Requests  int64     `json:"requests"`
	Rows      int64     `json:"rows"`
	Errors    int64     `json:"errors"`
	Throttled int64     `json:"throttled"`
}

// UsageSeries is the data API's usage from `since` on, in buckets of `bucket` (the ones with
// no request left out), oldest first.
func UsageSeries(ctx context.Context, q Querier, apiID int64, since time.Time, bucket time.Duration) ([]UsagePoint, error) {
	secs := int64(bucket / time.Second)
	rows, err := q.Query(ctx, `SELECT to_timestamp(floor(extract(epoch FROM minute) / $3) * $3) AT TIME ZONE 'UTC' AS at,
		sum(requests)::bigint, sum(rows)::bigint, sum(errors)::bigint, sum(throttled)::bigint
		FROM data_api_usage WHERE data_api_id = $1 AND minute >= $2 GROUP BY 1 ORDER BY 1`, apiID, since, secs)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (UsagePoint, error) {
		var p UsagePoint
		return p, r.Scan(&p.At, &p.Requests, &p.Rows, &p.Errors, &p.Throttled)
	})
	if out == nil {
		out = []UsagePoint{}
	}
	return out, err
}
