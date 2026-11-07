// Package dataapis is the data APIs' side of the gateway (docs/specs/data-apis.md): an object
// of a folder that publishes datasources as a read-only HTTP API, answered by pods of its own
// on Kubernetes. Here: what a definition holds and the checks it must pass (definition.go),
// its keys (keys.go), and its rows (store.go). A feature of the Go gateway only.
package dataapis

import (
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The states of a data API: made and edited as a draft, served while active, kept with its
// pods at zero while paused.
const (
	Draft  = "draft"
	Active = "active"
	Paused = "paused"
)

// Limits that do not depend on the installation.
const (
	MaxName          = 200
	MaxDescription   = 2000
	MaxResources     = 50
	MaxColumns       = 500
	DefaultPage      = 1000
	MaxPage          = 10000
	MaxSortColumns   = 5
	DefaultCPU       = "250m"
	DefaultMemory    = "256Mi"
	SourceDatasource = "datasource"
)

// Operators are the filters a column may allow, as the pods read them: `col=`, `col__in=`,
// `col__gt=` and so on, `col__null=`.
var Operators = []string{"eq", "in", "gt", "gte", "lt", "lte", "null"}

var rangeOperators = map[string]bool{"gt": true, "gte": true, "lt": true, "lte": true}

// a slug and a resource's name: a DNS label (they name Kubernetes objects and paths)
var labelRe = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)

// resource names the pods answer themselves: /data/{api}/openapi.json, /data/{api}/health
var reservedResources = map[string]bool{"health": true, "openapi": true}

// CheckSlug says what is wrong with a data API's address, "" when nothing.
func CheckSlug(slug string) string {
	if !labelRe.MatchString(slug) {
		return "The address is 1 to 63 lowercase letters, digits and dashes, not starting or ending with a dash"
	}
	return ""
}

// Runtime is how the pods of a data API run: a fixed number of replicas, or a minimum and a
// maximum with autoscaling on CPU; the CPU and memory of each pod. Only an administrator sets
// it (Administration → Data APIs, or the API's page when an administrator opens it): a new
// data API, and a copy, start from DefaultRuntime.
type Runtime struct {
	Autoscale   bool   `json:"autoscale"`
	Replicas    int    `json:"replicas"`
	MinReplicas int    `json:"min_replicas"`
	MaxReplicas int    `json:"max_replicas"`
	CPU         string `json:"cpu"`
	Memory      string `json:"memory"`
}

// The bounds of a runtime the administrator sets: not caps of the installation, only what a
// pod of DuckDB needs to prepare a table (below them it fails on a medium one) and what is
// reasonable to ask of a node. The pods of one data API are also at most the installation's
// DATA_API__MAX_TOTAL_REPLICAS, and MaxReplicasPerAPI (the status keeps MaxPodRows pods).
const (
	MinMillicores     = 100
	MaxMillicores     = 16_000
	MinMemoryBytes    = int64(256) << 20
	MaxMemoryBytes    = int64(64) << 30
	MinCPU            = "100m"
	MaxCPU            = "16"
	MinMemory         = "256Mi"
	MaxMemory         = "64Gi"
	MaxReplicasPerAPI = 40
)

// DefaultRuntime is the runtime of a new data API and of a copy.
func DefaultRuntime() Runtime {
	return Runtime{Replicas: 1, MinReplicas: 1, MaxReplicas: 1, CPU: DefaultCPU, Memory: DefaultMemory}
}

// MostReplicas is the most pods one data API may run on this installation: MaxReplicasPerAPI,
// or the most of all the active ones together when that is fewer.
func MostReplicas(maxTotal int) int {
	return max(min(MaxReplicasPerAPI, maxTotal), 1)
}

// Pods is the most pods the runtime may run.
func (r Runtime) Pods() int {
	if r.Autoscale {
		return r.MaxReplicas
	}
	return r.Replicas
}

// SameRuntime says whether two runtimes run the same pods: the same replicas (the ones used),
// the same CPU and memory, whatever the quantities' spelling («1» and «1000m»).
func SameRuntime(a, b Runtime) bool {
	if a.Autoscale != b.Autoscale {
		return false
	}
	if a.Autoscale && (a.MinReplicas != b.MinReplicas || a.MaxReplicas != b.MaxReplicas) {
		return false
	}
	if !a.Autoscale && a.Replicas != b.Replicas {
		return false
	}
	ca, errA := config.Millicores(a.CPU)
	cb, errB := config.Millicores(b.CPU)
	if errA != nil || errB != nil {
		if strings.TrimSpace(a.CPU) != strings.TrimSpace(b.CPU) {
			return false
		}
	} else if ca != cb {
		return false
	}
	ma, errA := config.MemoryBytes(a.Memory)
	mb, errB := config.MemoryBytes(b.Memory)
	if errA != nil || errB != nil {
		return strings.TrimSpace(a.Memory) == strings.TrimSpace(b.Memory)
	}
	return ma == mb
}

// CheckRuntime checks a runtime against the bounds above, with at most MostReplicas(maxTotal)
// pods; it is returned tidied (the quantities trimmed, the unused numbers in line with the
// used ones).
func CheckRuntime(r Runtime, maxTotal int) (Runtime, error) {
	r.CPU, r.Memory = strings.TrimSpace(r.CPU), strings.TrimSpace(r.Memory)
	most := MostReplicas(maxTotal)
	if r.Autoscale {
		if r.MinReplicas < 1 || r.MaxReplicas < r.MinReplicas {
			return r, errors.New("with autoscaling, the replicas go from a minimum of at least 1 to a maximum not below it")
		}
		if r.MaxReplicas > most {
			return r, fmt.Errorf("at most %d replicas per data API", most)
		}
		r.Replicas = r.MinReplicas
	} else {
		if r.Replicas < 1 {
			return r, errors.New("at least 1 replica")
		}
		if r.Replicas > most {
			return r, fmt.Errorf("at most %d replicas per data API", most)
		}
		r.MinReplicas, r.MaxReplicas = r.Replicas, r.Replicas
	}
	cpu, err := config.Millicores(r.CPU)
	if err != nil {
		return r, err
	}
	if cpu < MinMillicores {
		return r, fmt.Errorf("at least %s of CPU per pod", MinCPU)
	}
	if cpu > MaxMillicores {
		return r, fmt.Errorf("at most %s of CPU per pod", MaxCPU)
	}
	mem, err := config.MemoryBytes(r.Memory)
	if err != nil {
		return r, err
	}
	if mem < MinMemoryBytes {
		return r, fmt.Errorf("at least %s of memory per pod", MinMemory)
	}
	if mem > MaxMemoryBytes {
		return r, fmt.Errorf("at most %s of memory per pod", MaxMemory)
	}
	return r, nil
}

// Source is where a resource's rows come from: a kind and an id. Only datasources for now.
type Source struct {
	Kind string `json:"kind"`
	ID   int64  `json:"id"`
}

// Filter is a column one may filter on, and the operators allowed on it.
type Filter struct {
	Column    string   `json:"column"`
	Operators []string `json:"operators"`
}

// Resource is one path of a data API: /data/{slug}/{name}.
type Resource struct {
	Name        string   `json:"name"`
	Source      Source   `json:"source"`
	Columns     []string `json:"columns"`
	Filters     []Filter `json:"filters"`
	DefaultSort []string `json:"default_sort"`
	MaxPage     int      `json:"max_page"`
}

// Definition is what a version holds: the resources and the runtime.
type Definition struct {
	Resources []Resource `json:"resources"`
	Runtime   Runtime    `json:"runtime"`
}

// Column is a column of a datasource, its type as the datasource lists it (Polars' names).
type Column struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

// Datasource is what the checks need of a datasource.
type Datasource struct {
	ID        int64
	Name      string
	ProjectID int64
	Columns   []Column
}

// filterable says which operators a column's type allows: nil when it cannot be filtered on.
// The kind is the one the pods give the column once they read its parquet (lists, structures,
// binary columns cannot be filtered on; ranges only on numbers, dates and times).
func filterable(typ string) map[string]bool {
	kind := dataapiwire.KindOfPolars(typ)
	if kind == dataapiwire.KindOther {
		return nil
	}
	ops := map[string]bool{"eq": true, "in": true, "null": true}
	switch kind {
	case dataapiwire.KindInt, dataapiwire.KindFloat, dataapiwire.KindDecimal, dataapiwire.KindDate,
		dataapiwire.KindTimestamp, dataapiwire.KindTimestampTZ, dataapiwire.KindTime:
		for op := range rangeOperators {
			ops[op] = true
		}
	}
	return ops
}

// CheckDefinition checks the resources of a definition against their datasources (`sources`,
// by id; a missing one is a datasource that does not exist or that may not be read) and
// returns them tidied: the lists without duplicates, the operators in a stable order, the
// page at its default when not given.
func CheckDefinition(def Definition, sources map[int64]*Datasource) (Definition, error) {
	if len(def.Resources) > MaxResources {
		return def, fmt.Errorf("at most %d resources per data API", MaxResources)
	}
	seen := map[string]bool{}
	out := make([]Resource, 0, len(def.Resources))
	for _, r := range def.Resources {
		r.Name = strings.TrimSpace(r.Name)
		if !labelRe.MatchString(r.Name) {
			return def, fmt.Errorf("the resource «%s»: a name is 1 to 63 lowercase letters, digits and dashes", r.Name)
		}
		if reservedResources[r.Name] {
			return def, fmt.Errorf("the resource «%s»: the name is kept for the API itself", r.Name)
		}
		if seen[r.Name] {
			return def, fmt.Errorf("two resources are called «%s»", r.Name)
		}
		seen[r.Name] = true
		checked, err := checkResource(r, sources)
		if err != nil {
			return def, fmt.Errorf("the resource «%s»: %w", r.Name, err)
		}
		out = append(out, checked)
	}
	def.Resources = out
	return def, nil
}

func checkResource(r Resource, sources map[int64]*Datasource) (Resource, error) {
	if r.Source.Kind == "" {
		r.Source.Kind = SourceDatasource
	}
	if r.Source.Kind != SourceDatasource {
		return r, fmt.Errorf("a source of kind «%s» is not available", r.Source.Kind)
	}
	ds := sources[r.Source.ID]
	if ds == nil {
		return r, errors.New("the datasource does not exist, or you may not read it")
	}
	types := map[string]string{}
	for _, c := range ds.Columns {
		types[c.Name] = c.Type
	}
	if len(r.Columns) == 0 {
		return r, errors.New("choose at least one column that goes out")
	}
	if len(r.Columns) > MaxColumns {
		return r, fmt.Errorf("at most %d columns", MaxColumns)
	}
	out := []string{}
	going := map[string]bool{}
	for _, c := range r.Columns {
		if _, ok := types[c]; !ok {
			return r, fmt.Errorf("the column «%s» is not in the datasource «%s»", c, ds.Name)
		}
		if !going[c] {
			going[c] = true
			out = append(out, c)
		}
	}
	r.Columns = out
	filters := []Filter{}
	filtered := map[string]bool{}
	for _, f := range r.Filters {
		if !going[f.Column] {
			return r, fmt.Errorf("the column «%s» is filtered on but does not go out", f.Column)
		}
		if filtered[f.Column] {
			return r, fmt.Errorf("the column «%s» has two filters", f.Column)
		}
		filtered[f.Column] = true
		allowed := filterable(types[f.Column])
		if allowed == nil {
			return r, fmt.Errorf("the column «%s» (%s) cannot be filtered on", f.Column, types[f.Column])
		}
		if len(f.Operators) == 0 {
			return r, fmt.Errorf("the column «%s»: choose at least one operator", f.Column)
		}
		for _, op := range f.Operators {
			if !slices.Contains(Operators, op) {
				return r, fmt.Errorf("«%s» is not an operator (%s)", op, strings.Join(Operators, ", "))
			}
		}
		ops := []string{}
		for _, op := range Operators {
			if !slices.Contains(f.Operators, op) {
				continue
			}
			if !allowed[op] {
				return r, fmt.Errorf("the column «%s» (%s) cannot be compared with «%s»: only numbers and dates can", f.Column, types[f.Column], op)
			}
			ops = append(ops, op)
		}
		filters = append(filters, Filter{Column: f.Column, Operators: ops})
	}
	r.Filters = filters
	if len(r.DefaultSort) > MaxSortColumns {
		return r, fmt.Errorf("the default order has at most %d columns", MaxSortColumns)
	}
	sort := []string{}
	sorted := map[string]bool{}
	for _, s := range r.DefaultSort {
		col := strings.TrimPrefix(s, "-")
		if !filtered[col] {
			return r, fmt.Errorf("the default order is on «%s», which is not among the columns one may filter on", col)
		}
		if sorted[col] {
			return r, fmt.Errorf("the default order names «%s» twice", col)
		}
		sorted[col] = true
		sort = append(sort, s)
	}
	r.DefaultSort = sort
	if r.MaxPage == 0 {
		r.MaxPage = DefaultPage
	}
	if r.MaxPage < 1 || r.MaxPage > MaxPage {
		return r, fmt.Errorf("the largest page is from 1 to %d rows", MaxPage)
	}
	return r, nil
}

// SameResource says whether two resources publish the same thing: what a person who may not
// read the datasource can leave as it is.
func SameResource(a, b Resource) bool {
	return a.Name == b.Name && a.Source == b.Source && slices.Equal(a.Columns, b.Columns) &&
		slices.EqualFunc(a.Filters, b.Filters, func(x, y Filter) bool { return x.Column == y.Column && slices.Equal(x.Operators, y.Operators) }) &&
		slices.Equal(a.DefaultSort, b.DefaultSort) && a.MaxPage == b.MaxPage
}

// Sentence is an error's text as a detail: a capital, the rest as it is.
func Sentence(s string) string {
	if s == "" {
		return s
	}
	r := []rune(s)
	return strings.ToUpper(string(r[:1])) + string(r[1:])
}
