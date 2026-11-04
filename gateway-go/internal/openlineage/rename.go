package openlineage

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
)

// ── renames and moves: the new name in the catalog, the old one removed ─────────────────────

// Ref is an object whose catalog name is taken: a flow, a datasource or one of OBJECT_KINDS,
// with the folder and the name it has now.
type Ref struct {
	Kind          string
	ID, ProjectID int64
	Name          string
}

// Name is one of the names catalog_names takes, in the order it takes them.
type Name struct {
	Kind string
	ID   int64
	Name string
}

// Names is catalog_names' dict: (kind, id) → the name, in insertion order.
type Names []Name

func (n Names) set(kind string, id int64, name string) Names {
	for i := range n {
		if n[i].Kind == kind && n[i].ID == id {
			n[i].Name = name
			return n
		}
	}
	return append(n, Name{kind, id, name})
}

// OldName is an old job or dataset to remove: (jobs | datasets, namespace, name).
type OldName struct{ Kind, Namespace, Name string }

// currentName is _current_name.
func (b *Builder) currentName(ctx context.Context, r Ref) (string, error) {
	switch r.Kind {
	case KindDatasource:
		return b.DatasourceName(ctx, &Datasource{ProjectID: r.ProjectID, Name: r.Name})
	case KindFlow:
		return b.JobNameForFlow(ctx, &Flow{ProjectID: r.ProjectID, Name: r.Name})
	}
	return b.JobNameFor(ctx, r.Kind, &Object{ProjectID: r.ProjectID, Name: r.Name})
}

// CatalogNames is catalog_names: the names these objects have in the catalog NOW, taken
// before a rename or a move and handed to Renamed after the commit. Empty when OpenLineage is off.
func (e *Emitter) CatalogNames(ctx context.Context, q perm.Querier, refs ...Ref) (Names, error) {
	if !e.Enabled() {
		return nil, nil
	}
	b := e.Builder(q)
	var out Names
	for _, r := range refs {
		n, err := b.currentName(ctx, r)
		if err != nil {
			return nil, err
		}
		out = out.set(r.Kind, r.ID, n)
	}
	return out, nil
}

// FolderNames is folder_names: catalog_names of everything in a folder and its subfolders.
func (e *Emitter) FolderNames(ctx context.Context, q perm.Querier, projectID int64) (Names, error) {
	if !e.Enabled() {
		return nil, nil
	}
	rows, err := q.Query(ctx, `SELECT id, parent_id FROM projects`)
	if err != nil {
		return nil, err
	}
	parents := map[int64]*int64{}
	for rows.Next() {
		var id int64
		var parent *int64
		if err := rows.Scan(&id, &parent); err != nil {
			rows.Close()
			return nil, err
		}
		parents[id] = parent
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	var ids []int64
	for id := range perm.Descendants(parents, map[int64]bool{projectID: true}) {
		ids = append(ids, id)
	}
	var refs []Ref
	for _, kind := range []string{KindFlow, KindDatasource, KindSQLScript, KindSavedScript, KindNotebook, KindFlowGroup} {
		rows, err := q.Query(ctx, fmt.Sprintf(`SELECT id, project_id, name FROM %s WHERE project_id = ANY($1)`, kindTable(kind)), ids)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			r := Ref{Kind: kind}
			if err := rows.Scan(&r.ID, &r.ProjectID, &r.Name); err != nil {
				rows.Close()
				return nil, err
			}
			refs = append(refs, r)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return nil, err
		}
	}
	return e.CatalogNames(ctx, q, refs...)
}

// Renamed is renamed: after the commit of a rename or a move, with the names taken before.
// Whatever changed name, the catalog gets under the new name and loses under the old one. The
// events are built and sent by the sending goroutine, with a connection of its own. Never fails.
func (e *Emitter) Renamed(ctx context.Context, q perm.Querier, before Names) {
	if len(before) == 0 || !e.Enabled() {
		return
	}
	b := e.Builder(q)
	var changed Names
	for _, n := range before {
		var r Ref
		err := q.QueryRow(ctx, fmt.Sprintf(`SELECT id, project_id, name FROM %s WHERE id = $1`, kindTable(n.Kind)), n.ID).
			Scan(&r.ID, &r.ProjectID, &r.Name)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				continue
			}
			e.Log.Error(fmt.Sprintf("openlineage: rename events not built: %v", err))
			return
		}
		r.Kind = n.Kind
		now, err := b.currentName(ctx, r)
		if err != nil {
			e.Log.Error(fmt.Sprintf("openlineage: rename events not built: %v", err))
			return
		}
		if now != n.Name {
			changed = append(changed, n)
		}
	}
	if len(changed) > 0 {
		e.enqueue(item{label: "rename", build: func(ctx context.Context) ([]pyjson.Object, []OldName, error) {
			return e.renameEvents(ctx, changed)
		}})
	}
}

// renameEvents is _rename_events: the events that tell the catalog the new names, and the old
// names to remove.
func (e *Emitter) renameEvents(ctx context.Context, changed Names) ([]pyjson.Object, []OldName, error) {
	b := e.Builder(e.DB)
	ns := b.S.Namespace
	dsNamespace := "tabularia://" + ns
	var events []pyjson.Object
	var old []OldName
	sent := map[string]bool{}
	var renamedDS []*Datasource
	for _, n := range changed {
		switch n.Kind {
		case KindFlow:
			f, err := b.GetFlow(ctx, n.ID)
			if err != nil {
				return nil, nil, err
			}
			if f == nil {
				continue
			}
			ev, err := b.JobEventsForFlow(ctx, f)
			if err != nil {
				return nil, nil, err
			}
			events = append(events, ev...)
			sent[fmt.Sprintf("flow/%d", f.ID)] = true
			old = append(old, OldName{"jobs", ns, n.Name})
			labels, err := b.outputLabels(ctx, f.ID)
			if err != nil {
				return nil, nil, err
			}
			for _, l := range append(labels, stepLabels(f)...) {
				old = append(old, OldName{"jobs", ns, n.Name + "." + l})
			}
			emails, err := b.oldEmailDatasets(ctx, f, n.Name)
			if err != nil {
				return nil, nil, err
			}
			old = append(old, emails...)
		case KindDatasource:
			ds, err := b.GetDatasource(ctx, n.ID)
			if err != nil {
				return nil, nil, err
			}
			if ds == nil {
				continue
			}
			dataset, err := b.DatasourceDataset(ctx, ds, false, nil)
			if err != nil {
				return nil, nil, err
			}
			facets := append(pyjson.Object{}, dataset.Facets...).Set("lifecycleStateChange", facet(std("1-0-1", "LifecycleStateChangeDatasetFacet"),
				pyjson.KV{Key: "lifecycleStateChange", Value: "RENAME"},
				pyjson.KV{Key: "previousIdentifier", Value: pyjson.Object{{Key: "name", Value: n.Name}, {Key: "namespace", Value: dsNamespace}}}))
			events = append(events, pyjson.Object{{Key: "eventTime", Value: Now()}, {Key: "producer", Value: Producer},
				{Key: "schemaURL", Value: "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/DatasetEvent"},
				{Key: "dataset", Value: pyjson.Object{{Key: "namespace", Value: dsNamespace}, {Key: "name", Value: dataset.Name}, {Key: "facets", Value: facets}}}})
			old = append(old, OldName{"datasets", dsNamespace, n.Name})
			if ds.Kind == "database" {
				ev, err := b.refreshJobEvent(ctx, ds)
				if err != nil {
					return nil, nil, err
				}
				events = append(events, ev)
				old = append(old, OldName{"jobs", ns, tail(n.Name) + " (refresh)"})
			}
			renamedDS = append(renamedDS, ds)
		default:
			o, err := b.GetObject(ctx, n.Kind, n.ID)
			if err != nil {
				return nil, nil, err
			}
			if o == nil {
				continue
			}
			ev, err := b.JobEventsFor(ctx, o)
			if err != nil {
				return nil, nil, err
			}
			events = append(events, ev...)
			sent[fmt.Sprintf("%s/%d", n.Kind, o.ID)] = true
			old = append(old, OldName{"jobs", ns, n.Name})
			if n.Kind == KindFlowGroup {
				for _, l := range groupCellJobs(o.Definition) {
					old = append(old, OldName{"jobs", ns, n.Name + "." + l})
				}
				emails, err := b.oldPushmailDatasets(ctx, o, n.Name)
				if err != nil {
					return nil, nil, err
				}
				old = append(old, emails...)
			}
		}
	}
	flows, err := b.flowsTouching(ctx, renamedDS)
	if err != nil {
		return nil, nil, err
	}
	for _, f := range flows {
		if !sent[fmt.Sprintf("flow/%d", f.ID)] {
			ev, err := b.JobEventsForFlow(ctx, f)
			if err != nil {
				return nil, nil, err
			}
			events = append(events, ev...)
		}
	}
	readers, err := b.readersTouching(ctx, renamedDS)
	if err != nil {
		return nil, nil, err
	}
	for _, o := range readers {
		if !sent[fmt.Sprintf("%s/%d", o.Kind, o.ID)] {
			ev, err := b.JobEventsFor(ctx, o)
			if err != nil {
				return nil, nil, err
			}
			events = append(events, ev...)
		}
	}
	return events, old, nil
}

// refreshJobEvent is _refresh_job_event: the refresh of a database datasource as a static job.
func (b *Builder) refreshJobEvent(ctx context.Context, ds *Datasource) (pyjson.Object, error) {
	job, inputs, err := b.refreshJob(ctx, ds)
	if err != nil {
		return nil, err
	}
	out, err := b.DatasourceDataset(ctx, ds, true, nil)
	if err != nil {
		return nil, err
	}
	return b.JobEvent(job, inputs, []Dataset{out}), nil
}

// outputLabels is _output_labels: a flow's Output jobs, one per destination its runs wrote to.
func (b *Builder) outputLabels(ctx context.Context, flowID int64) ([]string, error) {
	rows, err := b.Q.Query(ctx, `SELECT DISTINCT publish_name, destination, email FROM runs WHERE flow_id = $1 AND kind = 'flow' LIMIT 200`, flowID)
	if err != nil {
		return nil, err
	}
	type row struct{ p, d, e *string }
	var list []row
	for rows.Next() {
		var x row
		if err := rows.Scan(&x.p, &x.d, &x.e); err != nil {
			rows.Close()
			return nil, err
		}
		list = append(list, x)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	var labels []string
	for _, x := range list {
		if str(x.p) != "" || str(x.d) != "" || str(x.e) != "" {
			l, err := outputLabel(0, x.p, x.d, x.e)
			if err != nil {
				return nil, err
			}
			dup := false
			for _, y := range labels {
				dup = dup || y == l
			}
			if !dup {
				labels = append(labels, l)
			}
		}
	}
	return labels, nil
}

func (b *Builder) smtpNamespace(c *Connection) string {
	port := int64(587)
	if c.Port != nil && *c.Port != 0 {
		port = *c.Port
	}
	return EmailDataset(c.Host, port, "", "").Namespace
}

// oldEmailDatasets is _old_email_datasets.
func (b *Builder) oldEmailDatasets(ctx context.Context, f *Flow, oldName string) ([]OldName, error) {
	nodes, err := nodesOf(definition(f))
	if err != nil {
		return nil, err
	}
	var out []OldName
	for _, n := range nodes {
		d, err := dataOf(n)
		if err != nil {
			return nil, err
		}
		if n.Value("type") != "output" || d.Value("destType") != "email" || !pyjson.Truthy(d.Value("connectionId")) {
			continue
		}
		c, err := b.connection(ctx, d.Value("connectionId"))
		if err != nil {
			return nil, err
		}
		if c != nil {
			x := OldName{"datasets", b.smtpNamespace(c), "/" + strings.Trim(oldName, "/") + "/email"}
			dup := false
			for _, y := range out {
				dup = dup || y == x
			}
			if !dup {
				out = append(out, x)
			}
		}
	}
	return out, nil
}

// oldPushmailDatasets is _old_pushmail_datasets.
func (b *Builder) oldPushmailDatasets(ctx context.Context, g *Object, oldName string) ([]OldName, error) {
	suffix := " (" + kindWord(KindFlowGroup) + ")"
	oldGroup := strings.TrimSuffix(oldName, suffix)
	var out []OldName
	for _, cell := range pushmailCells(g.Definition) {
		if cell.conn == nil {
			continue
		}
		c, err := b.GetConnection(ctx, *cell.conn)
		if err != nil {
			return nil, err
		}
		if c == nil {
			continue
		}
		label := strings.TrimSpace(cell.label)
		if label == "" {
			label = "Pushmail"
		}
		out = append(out, OldName{"datasets", b.smtpNamespace(c), pushmailDatasetName("", oldGroup, label)})
	}
	return out, nil
}

// flowsTouching is _flows_touching: the flows that read one of these datasources (by id) or
// write one of them (by folder and current name).
func (b *Builder) flowsTouching(ctx context.Context, list []*Datasource) ([]*Flow, error) {
	if len(list) == 0 {
		return nil, nil
	}
	ids := map[string]bool{}
	written := map[string]bool{}
	for _, d := range list {
		ids[hashKey(d.ID)] = true
		written[hashKey(d.ProjectID)+"\x00"+d.Name] = true
	}
	rows, err := b.Q.Query(ctx, `SELECT id, project_id, name, description, definition FROM flows`)
	if err != nil {
		return nil, err
	}
	var all []*Flow
	for rows.Next() {
		var f Flow
		if err := rows.Scan(&f.ID, &f.ProjectID, &f.Name, &f.Description, &f.Definition); err != nil {
			rows.Close()
			return nil, err
		}
		all = append(all, &f)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	var out []*Flow
	for _, f := range all {
		nodes, err := nodesOf(definition(f))
		if err != nil {
			return nil, err
		}
		for _, n := range nodes {
			d, err := dataOf(n)
			if err != nil {
				return nil, err
			}
			if n.Value("type") == "source" && ids[hashKey(d.Value("datasourceId"))] {
				out = append(out, f)
				break
			}
			if n.Value("type") == "output" && pyjson.Or(d.Value("destType"), "datasource") == "datasource" {
				if written[hashKey(d.Value("projectId"))+"\x00"+pyd.Strip(text(d.Value("name")))] {
					out = append(out, f)
					break
				}
			}
		}
	}
	return out, nil
}

// readersTouching is _readers_touching: the saved scripts that declare one of these
// datasources and the notebooks that have read one.
func (b *Builder) readersTouching(ctx context.Context, list []*Datasource) ([]*Object, error) {
	if len(list) == 0 {
		return nil, nil
	}
	ids := map[string]bool{}
	var plain []int64
	for _, d := range list {
		ids[hashKey(d.ID)] = true
		plain = append(plain, d.ID)
	}
	rows, err := b.Q.Query(ctx, `SELECT id, project_id, name, description, current_version FROM saved_scripts`)
	if err != nil {
		return nil, err
	}
	type script struct {
		o       Object
		content *string
	}
	var scripts []script
	var sids []int64
	for rows.Next() {
		x := script{o: Object{Kind: KindSavedScript}}
		if err := rows.Scan(&x.o.ID, &x.o.ProjectID, &x.o.Name, &x.o.Description, &x.o.CurrentVersion); err != nil {
			rows.Close()
			return nil, err
		}
		scripts = append(scripts, x)
		sids = append(sids, x.o.ID)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(scripts) > 0 { // lineage.current_contents: each script's current version, read at once
		crows, err := b.Q.Query(ctx, `SELECT v.script_id, v.content FROM saved_script_versions v JOIN saved_scripts s
			ON s.id = v.script_id AND s.current_version = v.version WHERE s.id = ANY($1)`, sids)
		if err != nil {
			return nil, err
		}
		raw := map[int64]*string{}
		for crows.Next() {
			var id int64
			var c string
			if err := crows.Scan(&id, &c); err != nil {
				crows.Close()
				return nil, err
			}
			raw[id] = &c
		}
		crows.Close()
		for i := range scripts {
			scripts[i].content = raw[scripts[i].o.ID]
		}
	}
	var out []*Object
	for i := range scripts {
		content, err := savedscripts.Over(savedscripts.EmptyContent(), scripts[i].content)
		if err != nil {
			return nil, err
		}
		inputs, _ := content.Value("inputs").([]any)
		for _, in := range inputs {
			if o, ok := in.(pyjson.Object); ok && ids[hashKey(o.Value("datasource_id"))] {
				out = append(out, &scripts[i].o)
				break
			}
		}
	}
	nrows, err := b.Q.Query(ctx, `SELECT DISTINCT notebook_id FROM notebook_reads WHERE datasource_id = ANY($1) ORDER BY notebook_id`, plain)
	if err != nil {
		return nil, err
	}
	var nbs []int64
	for nrows.Next() {
		var id int64
		if err := nrows.Scan(&id); err != nil {
			nrows.Close()
			return nil, err
		}
		nbs = append(nbs, id)
	}
	nrows.Close()
	for _, id := range nbs {
		o, err := b.GetObject(ctx, KindNotebook, id)
		if err != nil {
			return nil, err
		}
		if o != nil {
			out = append(out, o)
		}
	}
	return out, nil
}

// marquezAPI is _marquez_api: Marquez's REST API, when the collector is Marquez.
func marquezAPI(s Settings) string {
	if s.URL == "" || strings.Trim(s.Endpoint, "/") != "api/v1/lineage" {
		return ""
	}
	return strings.TrimRight(s.URL, "/") + "/api/v1"
}

// quote is urllib.parse.quote(s, safe=”).
func quote(s string) string {
	var b strings.Builder
	for _, c := range []byte(s) {
		if c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-' || c == '~' {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// removeOldNames is _remove_old_names: deletes old jobs and datasets from Marquez (a soft
// delete); a name already gone counts as removed.
func removeOldNames(ctx context.Context, s Settings, old []OldName, log *slog.Logger) int {
	api := marquezAPI(s)
	if len(old) == 0 || api == "" {
		return 0
	}
	client := &http.Client{Timeout: time.Duration(s.Timeout * float64(time.Second))}
	removed := 0
	for _, o := range old {
		req, err := http.NewRequestWithContext(ctx, http.MethodDelete, api+"/namespaces/"+quote(o.Namespace)+"/"+o.Kind+"/"+quote(o.Name), nil)
		if err != nil {
			log.Warn(fmt.Sprintf("openlineage: old %s «%s» not removed: %s", strings.TrimSuffix(o.Kind, "s"), o.Name, err))
			continue
		}
		if s.APIKey != "" {
			req.Header.Set("Authorization", "Bearer "+s.APIKey)
		}
		res, err := client.Do(req)
		if err != nil {
			log.Warn(fmt.Sprintf("openlineage: old %s «%s» not removed: %s", strings.TrimSuffix(o.Kind, "s"), o.Name, err))
			continue
		}
		res.Body.Close()
		if res.StatusCode < 300 || res.StatusCode == 404 {
			removed++
		} else {
			log.Warn(fmt.Sprintf("openlineage: old %s «%s» not removed: HTTP %d", strings.TrimSuffix(o.Kind, "s"), o.Name, res.StatusCode))
		}
	}
	return removed
}

// tail is text[1:].
func tail(s string) string {
	if s == "" {
		return ""
	}
	return s[1:]
}
