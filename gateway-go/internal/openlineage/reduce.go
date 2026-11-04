package openlineage

import (
	"fmt"
	"log/slog"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// The datasets' reduction (openlineage/client/dataset): with dataset.reducing_enabled, a
// RunEvent's datasets whose names end with a partition (key=value, a date, yyyy/MM[/dd],
// yyyyMM) are named without it, and the same dataset read or written under several
// partitions is one, with its locations in a `subset` facet.

const trimmerPrefix = "openlineage.client.dataset.trimmers."

type trimmer struct {
	fqn  string
	trim func(string) string
}

func hasMultipleDirectories(name string) bool {
	if name == "" || !strings.Contains(name, "/") {
		return false
	}
	n := 0
	for _, p := range strings.Split(name, "/") {
		if p != "" {
			n++
		}
	}
	return n > 1
}

func lastPart(name string) string {
	if name == "" || !strings.Contains(name, "/") {
		return name
	}
	s := strings.TrimRight(name, "/")
	return s[strings.LastIndex(s, "/")+1:]
}

func removeLastPart(name string, parts int) string {
	s := strings.TrimRight(name, "/")
	if !strings.Contains(s, "/") {
		return name
	}
	for i := 0; i < parts; i++ {
		idx := strings.LastIndex(s, "/")
		if idx == -1 {
			break
		}
		s = s[:idx]
	}
	return s
}

// validDate is datetime.strptime(text, fmt) not raising, for %Y, %m, %d and literal separators.
func validDate(text, format string) bool {
	y, m, d := 1900, 1, 1
	i := 0
	digits := func(min, max int) (int, bool) {
		j := i
		for j < len(text) && j-i < max && text[j] >= '0' && text[j] <= '9' {
			j++
		}
		if j-i < min {
			return 0, false
		}
		n, _ := strconv.Atoi(text[i:j])
		i = j
		return n, true
	}
	for k := 0; k < len(format); k++ {
		if format[k] == '%' && k+1 < len(format) {
			k++
			var ok bool
			switch format[k] {
			case 'Y':
				y, ok = digits(4, 4)
			case 'm':
				m, ok = digits(1, 2)
			case 'd':
				d, ok = digits(1, 2)
			}
			if !ok {
				return false
			}
			continue
		}
		if i >= len(text) || text[i] != format[k] {
			return false
		}
		i++
	}
	if i != len(text) || m < 1 || m > 12 || d < 1 || y < 1 {
		return false
	}
	t := time.Date(y, time.Month(m), d, 0, 0, 0, 0, time.UTC)
	return t.Day() == d
}

var (
	datePatterns = []struct {
		format string
		re     *regexp.Regexp
	}{{"%Y-%m-%d", regexp.MustCompile(`\d{4}-\d{2}-\d{2}`)}, {"%d.%m.%Y", regexp.MustCompile(`\d{2}\.\d{2}\.\d{4}`)},
		{"%Y%m%d", regexp.MustCompile(`\d{8}`)}}
	dateNoise = regexp.MustCompile(`[TZ\s:.\-\d]*`)
	yearMonth = []struct {
		re     *regexp.Regexp
		format string
	}{{regexp.MustCompile(`^\d{4}\d{2}$`), "%Y%m"}, {regexp.MustCompile(`^\d{4}-\d{2}$`), "%Y-%m"}}
)

func looksLikeDate(segment string) bool {
	for _, p := range datePatterns {
		for _, match := range p.re.FindAllString(segment, -1) {
			if validDate(match, p.format) {
				left := dateNoise.ReplaceAllString(strings.Replace(segment, match, "", 1), "")
				if left == "" {
					return true
				}
			}
		}
	}
	return false
}

var builtinTrimmers = []trimmer{
	{trimmerPrefix + "KeyValueTrimmer", func(name string) string {
		if !hasMultipleDirectories(name) || strings.Count(lastPart(name), "=") != 1 {
			return name
		}
		return removeLastPart(name, 1)
	}},
	{trimmerPrefix + "DateTrimmer", func(name string) string {
		if hasMultipleDirectories(name) && looksLikeDate(lastPart(name)) {
			return removeLastPart(name, 1)
		}
		return name
	}},
	{trimmerPrefix + "MultiDirDateTrimmer", func(name string) string {
		parts := strings.Split(strings.Trim(name, "/"), "/")
		if len(parts) == 3 && validDate(parts[1]+"/"+parts[2], "%Y/%m") {
			return removeLastPart(name, 2)
		}
		if len(parts) >= 4 {
			n := len(parts)
			if validDate(parts[n-3]+"/"+parts[n-2]+"/"+parts[n-1], "%Y/%m/%d") {
				return removeLastPart(name, 3)
			}
			if validDate(parts[n-2]+"/"+parts[n-1], "%Y/%m") {
				return removeLastPart(name, 2)
			}
		}
		return name
	}},
	{trimmerPrefix + "YearMonthTrimmer", func(name string) string {
		if !hasMultipleDirectories(name) {
			return name
		}
		last := lastPart(name)
		for _, p := range yearMonth {
			if p.re.MatchString(last) && validDate(last, p.format) {
				return removeLastPart(name, 1)
			}
		}
		return name
	}},
}

type datasetReducer struct {
	trimmers []trimmer
	log      *slog.Logger
}

// newReducer is DatasetReducer: the built-in trimmers but those disabled; the extra ones are
// Python classes, none importable here (the library skips them with a warning, as here).
func newReducer(c *libConfig, log *slog.Logger) *datasetReducer {
	r := &datasetReducer{log: log}
	known := map[string]bool{}
	for _, t := range builtinTrimmers {
		known[t.fqn] = true
	}
	disabled := map[string]bool{}
	for _, d := range c.disabledTrimmers {
		disabled[d] = true
		if !known[d] {
			log.Warn(fmt.Sprintf("Configured disabled trimmer '%s' does not match any built-in DatasetNameTrimmer.", d))
		}
	}
	for _, t := range builtinTrimmers {
		if !disabled[t.fqn] {
			r.trimmers = append(r.trimmers, t)
		}
	}
	for _, x := range c.extraTrimmers {
		builtin := false
		for _, t := range builtinTrimmers {
			if t.fqn == x {
				builtin = true
				active := false
				for _, a := range r.trimmers {
					active = active || a.fqn == x
				}
				if !active { // a built-in one disabled and named again: at the end
					r.trimmers = append(r.trimmers, t)
				}
			}
		}
		if !builtin {
			log.Warn(fmt.Sprintf("Failed to load trimmer '%s': Failed to import %s. Skipping.", x, x))
		}
	}
	return r
}

// trimName is _trim.
func (r *datasetReducer) trimName(name string) (string, bool) {
	original, trimmed := name, false
	seen := map[string]bool{name: true}
	for pass := 0; pass < 16; pass++ {
		next := name
		for _, t := range r.trimmers {
			next = t.trim(next)
		}
		if next == name {
			return name, trimmed
		}
		if seen[next] {
			r.log.Warn(fmt.Sprintf("Cycle detected while trimming dataset name '%s': name '%s' was repeated after multiple trimmer passes. "+
				"Stopping trimming process and returning original name.", original, next))
			return original, false
		}
		seen[next] = true
		name, trimmed = next, true
	}
	r.log.Warn(fmt.Sprintf("Trimmer loop for dataset name '%s' did not converge after %d passes. One of the extra trimmers may not be "+
		"monotonically shortening. Returning original name.", original, 16))
	return original, false
}

type reduced struct {
	ds    pyjson.Object
	names map[string]bool
}

// reduce is reduce_datasets on a RunEvent: its inputs and its outputs.
func (r *datasetReducer) reduce(e pyjson.Object) pyjson.Object {
	for _, side := range []struct{ key, facets, facet, cond string }{
		{"inputs", "inputFacets", "InputSubsetInputDatasetFacet", "inputCondition"},
		{"outputs", "outputFacets", "OutputSubsetOutputDatasetFacet", "outputCondition"}} {
		list, _ := e.Value(side.key).([]any)
		if len(list) == 0 {
			continue
		}
		var groups []*reduced
		for _, d := range list {
			ds, _ := d.(pyjson.Object)
			original := text(ds.Value("name"))
			name, was := r.trimName(original)
			ds = append(pyjson.Object{}, ds...).Set("name", name)
			var hit *reduced
			for _, g := range groups {
				if sameDataset(g.ds, ds) {
					hit = g
					break
				}
			}
			if hit == nil {
				hit = &reduced{ds: ds, names: map[string]bool{}}
				groups = append(groups, hit)
			}
			if was {
				hit.names[original] = true
			}
		}
		out := []any{}
		for _, g := range groups {
			if len(g.names) == 0 {
				out = append(out, g.ds)
				continue
			}
			locs := make([]string, 0, len(g.names))
			for n := range g.names {
				locs = append(locs, n)
			}
			sort.Strings(locs)
			facets, _ := g.ds.Value(side.facets).(pyjson.Object)
			facets = append(pyjson.Object{}, facets...).Set("subset", facet(specs+"1-0-0/BaseSubsetDatasetFacet.json#/$defs/"+side.facet,
				pyjson.KV{Key: side.cond, Value: pyjson.Object{{Key: "locations", Value: stringsAny(locs)}, {Key: "type", Value: "location"}}}))
			out = append(out, append(pyjson.Object{}, g.ds...).Set(side.facets, facets))
		}
		e = e.Set(side.key, out)
	}
	return e
}

// sameDataset is ReducedDataset.matches: the namespace, the name and every facet equal.
func sameDataset(a, b pyjson.Object) bool {
	if a.Value("namespace") != b.Value("namespace") || a.Value("name") != b.Value("name") {
		return false
	}
	for _, k := range []string{"facets", "inputFacets", "outputFacets"} {
		x, _ := a.Value(k).(pyjson.Object)
		y, _ := b.Value(k).(pyjson.Object)
		if !sameJSON(x, y) {
			return false
		}
	}
	return true
}

func sameJSON(a, b pyjson.Object) bool {
	sa, _ := pyjson.Dumps(a, pyjson.Options{SortKeys: true})
	sb, _ := pyjson.Dumps(b, pyjson.Options{SortKeys: true})
	if len(a) == 0 && len(b) == 0 {
		return true
	}
	return sa == sb || reflect.DeepEqual(a, b)
}
