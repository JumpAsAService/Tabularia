package sqlscripts

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// Dialects is DIALECTS: the dialect a SQL on the catalog is READ in, by engine (the engine
// runs it).
var Dialects = map[string]string{"polars": "postgres", "duckdb": "duckdb", "chdb": "clickhouse", "clickhouse": "clickhouse",
	"bigquery": "bigquery"}

// DialectOf is DIALECTS.get(engine or "polars", "postgres").
func DialectOf(engine string) string {
	if engine == "" {
		engine = "polars"
	}
	if d, ok := Dialects[engine]; ok {
		return d
	}
	return "postgres"
}

// Refusal is _refuse: a 422 whose detail is a text.
type Refusal struct{ Message string }

func (r *Refusal) Error() string { return r.Message }

// Unreadable is sql_scripts.Unreadable: what a person may not read, said as what does not exist
// (a 422 too, which a dashboard tells apart).
type Unreadable struct{ Message string }

func (u *Unreadable) Error() string { return u.Message }

func refuse(format string, args ...any) error { return &Refusal{Message: fmt.Sprintf(format, args...)} }

// ErrInternal is where the Python code fails by itself: a 500.
var ErrInternal = errors.New("sql_scripts: the Python code fails here")

// Named is a datasource the SQL names, under its alias.
type Named struct {
	Alias      string
	Datasource Datasource
}

// Resolved is Resolved: the SQL the engine runs (no comments, the datasources as their
// aliases) and the datasources by alias, in the order named.
type Resolved struct {
	Query  string
	Tables []Named
}

func (r *Resolved) add(alias string, ds Datasource) {
	for i := range r.Tables {
		if r.Tables[i].Alias == alias {
			r.Tables[i].Datasource = ds // a dict keeps the first place
			return
		}
	}
	r.Tables = append(r.Tables, Named{Alias: alias, Datasource: ds})
}

// StripComments is sql_scripts.strip_comments: the SQL without its comments, quoted text left
// alone.
func StripComments(sql string) string {
	s := []rune(sql)
	var out []rune
	i, n := 0, len(s)
	startsWith := func(p string, at int) bool {
		r := []rune(p)
		if at+len(r) > n {
			return false
		}
		for k := range r {
			if s[at+k] != r[k] {
				return false
			}
		}
		return true
	}
	find := func(p string, from int) int {
		for k := from; k < n; k++ {
			if startsWith(p, k) {
				return k
			}
		}
		return -1
	}
	for i < n {
		c := s[i]
		switch {
		case c == '\'' || c == '"' || c == '`':
			j := i + 1
			for j < n {
				if s[j] == c {
					if j+1 < n && s[j+1] == c {
						j += 2
						continue
					}
					break
				}
				if s[j] == '\\' && c == '\'' {
					j += 2
					continue
				}
				j++
			}
			out = append(out, s[i:min(j+1, n)]...)
			i = j + 1
		case startsWith("--", i):
			j := find("\n", i)
			if j < 0 {
				i = n
			} else {
				i = j
			}
		case startsWith("/*", i):
			j := find("*/", i+2)
			out = append(out, ' ')
			if j < 0 {
				i = n
			} else {
				i = j + 2
			}
		default:
			out = append(out, c)
			i++
		}
	}
	return string(out)
}

// Quote is _quote: a name quoted in the dialect.
func Quote(name, dialect string) string {
	if dialect == "bigquery" {
		return "`" + strings.ReplaceAll(strings.ReplaceAll(name, `\`, `\\`), "`", "\\`") + "`"
	}
	return `"` + strings.ReplaceAll(name, `"`, `""`) + `"`
}

// pySplitlines are the line boundaries of Python's str.splitlines.
var pySplitlines = map[rune]bool{'\n': true, '\r': true, '\v': true, '\f': true, '\x1c': true, '\x1d': true, '\x1e': true,
	'\u0085': true, ' ': true, ' ': true}

// firstLine is `text.splitlines()[0]`; false where Python fails (no line at all).
func firstLine(text string) (string, bool) {
	if text == "" {
		return "", false
	}
	if i := strings.IndexFunc(text, func(r rune) bool { return pySplitlines[r] }); i >= 0 {
		return text[:i], true
	}
	return text, true
}

func folderName(names map[int64]string, id int64, def string) string {
	if n, ok := names[id]; ok {
		return n
	}
	return def
}

// Resolve is resolve: finds the datasources the SQL names, among those the person (`c`) may
// read, and puts their aliases in their place. A datasource the person cannot read is reported
// as one that does not exist (*Unreadable); the other refusals are *Refusal. The SQL is read by
// sqlglot, in the AI service.
func Resolve(ctx context.Context, q Querier, c *perm.Checker, svc *aisvc.Client, sql, engine string) (*Resolved, error) {
	text := pystr.Strip(StripComments(sql))
	for strings.HasSuffix(text, ";") {
		text = strings.TrimRightFunc(text[:len(text)-1], pystr.IsSpace)
	}
	if text == "" {
		return nil, refuse("The query is empty")
	}
	dialect := DialectOf(engine)
	parsed, err := svc.Tables(ctx, text, dialect)
	if err != nil {
		return nil, err
	}
	if parsed.Error != nil {
		line, ok := firstLine(*parsed.Error)
		if !ok {
			return nil, ErrInternal // str(e).splitlines()[0] of an empty message
		}
		return nil, refuse("The SQL could not be read: %s", pystr.Cut(line, 300))
	}
	if parsed.Statements != 1 {
		return nil, refuse("One query at a time: write a single SELECT")
	}
	if !parsed.Query {
		return nil, refuse("Only a query that reads data can run: start with SELECT or WITH")
	}
	readable, err := ReadableDatasources(ctx, c, q)
	if err != nil {
		return nil, err
	}
	all, err := Folders(ctx, q)
	if err != nil {
		return nil, err
	}
	return place(text, dialect, parsed, readable, all)
}

// place is the rest of resolve, once the SQL is read: each table named among the datasources
// `readable` (by name), in the folders `all`, and its alias put in its place.
func place(text, dialect string, parsed *aisvc.Parsed, readable []Datasource, all map[int64]Folder) (*Resolved, error) {
	ctes := map[string]bool{}
	for _, n := range parsed.CTEs {
		ctes[n] = true
	}
	byName := map[string][]Datasource{}
	for _, ds := range readable {
		k := pystr.Lower(ds.Name)
		byName[k] = append(byName[k], ds)
	}
	folders := make(map[int64]string, len(all))
	for id, f := range all {
		folders[id] = f.Name
	}
	type edit struct {
		start, end  int
		replacement string
	}
	var edits []edit
	runes := []rune(text)
	out := &Resolved{Query: text}
	for _, t := range parsed.Tables {
		name, folder := t.Name, t.Folder
		if t.Catalog {
			return nil, refuse("«%s»: name a datasource by its name, or by its folder and its name", t.Written)
		}
		if folder == "" && ctes[pystr.Lower(name)] {
			continue
		}
		matches := byName[pystr.Lower(name)]
		if folder != "" {
			var in []Datasource
			for _, d := range matches {
				if pystr.Lower(folderName(folders, d.ProjectID, "")) == pystr.Lower(folder) {
					in = append(in, d)
				}
			}
			matches = in
		}
		if len(matches) > 1 {
			var exact []Datasource
			for _, d := range matches {
				if d.Name == name {
					exact = append(exact, d)
				}
			}
			if len(exact) == 1 {
				matches = exact
			}
		}
		label := name
		if folder != "" {
			label = folder + "." + name
		}
		if len(matches) == 0 {
			return nil, &Unreadable{Message: fmt.Sprintf("There is no datasource «%s» you can read", label)}
		}
		if len(matches) > 1 && folder != "" {
			// the folder's name does not tell them apart either: only a new name will
			paths := Paths(all, " / ")
			where := make([]string, 0, len(matches))
			for _, d := range matches {
				p, ok := paths[d.ProjectID]
				if !ok {
					p = "?"
				}
				where = append(where, p)
			}
			sort.Strings(where)
			return nil, refuse("«%s» is in more than one folder called «%s» (%s): rename one of the folders, or one of the datasources",
				label, folder, strings.Join(where, ", "))
		}
		if len(matches) > 1 {
			where := make([]string, 0, len(matches))
			for _, d := range matches {
				where = append(where, folderName(folders, d.ProjectID, "?"))
			}
			sort.Strings(where)
			return nil, refuse("«%s» is in more than one folder (%s): write it as %s.%s", name, strings.Join(where, ", "),
				Quote(folderName(folders, matches[0].ProjectID, ""), dialect), Quote(name, dialect))
		}
		ds := matches[0]
		if ds.Key == "" {
			return nil, refuse("The datasource «%s» has no data yet", ds.Name)
		}
		if t.Start == nil || t.End == nil {
			return nil, refuse("«%s» could not be placed in the SQL: write its name in quotes", label)
		}
		start, end := *t.Start, *t.End
		if start < 0 || end >= len(runes) || start > end+1 {
			return nil, ErrInternal // offsets the text does not have: never from sqlglot
		}
		if start > 0 && strings.ContainsRune("\"`[", runes[start-1]) {
			start--
		}
		if end+1 < len(runes) && strings.ContainsRune("\"`]", runes[end+1]) {
			end++
		}
		alias := fmt.Sprintf("ds_%d", ds.ID)
		out.add(alias, ds)
		replacement := alias + " AS " + Quote(name, dialect)
		if t.Aliased {
			replacement = alias
		}
		edits = append(edits, edit{start, end, replacement})
	}
	// sorted(edits, reverse=True): by start, then end, then the text, the last first
	sort.SliceStable(edits, func(i, j int) bool {
		a, b := edits[i], edits[j]
		if a.start != b.start {
			return a.start > b.start
		}
		if a.end != b.end {
			return a.end > b.end
		}
		return a.replacement > b.replacement
	})
	for _, e := range edits {
		runes = append(append(append([]rune{}, runes[:e.start]...), []rune(e.replacement)...), runes[e.end+1:]...)
	}
	out.Query = string(runes)
	return out, nil
}

// CatalogChain is _catalog_chain: the engine's chain of a run on the catalog, the first
// datasource as the source and one `sql` operation reading every datasource under its alias.
func CatalogChain(r *Resolved) pyjson.Object {
	first := r.Tables[0].Datasource
	tables := pyjson.Object{}
	for _, n := range r.Tables {
		tables = tables.Set(n.Alias, pyjson.Object{{Key: "source", Value: pyjson.Object{
			{Key: "bucket", Value: n.Datasource.Bucket}, {Key: "key", Value: n.Datasource.Key}}}})
	}
	return pyjson.Object{{Key: "bucket", Value: first.Bucket}, {Key: "input_key", Value: first.Key},
		{Key: "operations", Value: []any{pyjson.Object{{Key: "type", Value: "sql"}, {Key: "params", Value: pyjson.Object{
			{Key: "query", Value: r.Query}, {Key: "tables", Value: tables}}}}}}}
}
