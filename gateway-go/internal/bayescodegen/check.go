package bayescodegen

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// This file is the check of the output (docs/specs/bayes-stan-codegen.md, «Safety checks on the
// output»), independent of how the text was written: the alphabet and the layout, the refused
// words, and a lexical scan of every call and every sampling statement against the catalogue's
// lists. Generate runs it on every program before it returns one; a failure is a bug of the codegen
// and no program is returned. stanc's --info is read by CrossCheck, in the stanc check of CI (lot S3).

// programByte reports the bytes of the program's alphabet: ^[A-Za-z0-9_ .,;:()\[\]{}<>=+\-*/^~\n?]*$.
func programByte(c byte) bool {
	switch {
	case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9':
		return true
	}
	return strings.IndexByte("_ .,;:()[]{}<>=+-*/^~\n?", c) >= 0
}

func wordByte(c byte) bool {
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_'
}

var refusedToken = func() map[string]bool {
	m := map[string]bool{}
	for _, w := range refusedTokens {
		m[w] = true
	}
	return m
}()

// CheckProgram checks a program's text: the alphabet and none of //, /* and */; the layout (a line
// feed after every line, no blank line, no trailing space, an indentation of two spaces a level);
// brackets balanced; no refused word as a token; every name followed by "(" a name the program may
// call (except the keyword for) and every name after "~" a statement of the catalogue.
func CheckProgram(text string) error {
	if text == "" || text[len(text)-1] != '\n' {
		return bug("the program does not end with a line feed")
	}
	for i := 0; i < len(text); i++ {
		c := text[i]
		if !programByte(c) {
			return bug("a byte outside the program's alphabet: %s", strconv.QuoteToASCII(string(c)))
		}
		if i+1 < len(text) {
			switch text[i : i+2] {
			case "//", "/*", "*/":
				return bug("a comment in the program")
			}
		}
	}
	if err := checkLayout(text); err != nil {
		return err
	}
	return scanTokens(text)
}

func checkLayout(text string) error {
	lines := strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	for n, l := range lines {
		where := "line " + strconv.Itoa(n+1)
		indent := len(l) - len(strings.TrimLeft(l, " "))
		switch {
		case strings.TrimSpace(l) == "":
			return bug("%s is blank", where)
		case l[len(l)-1] == ' ':
			return bug("%s ends with a space", where)
		case indent%2 != 0:
			return bug("%s is not indented by two spaces a level", where)
		}
	}
	return nil
}

// scanTokens reads the text as Stan's lexer would, enough to find every name and what follows it.
func scanTokens(s string) error {
	var open []byte
	prev := byte(0) // the last byte that is not a space
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch c {
		case '(', '[', '{':
			open = append(open, c)
		case ')', ']', '}':
			want := map[byte]byte{')': '(', ']': '[', '}': '{'}[c]
			if len(open) == 0 || open[len(open)-1] != want {
				return bug("unbalanced brackets in the program")
			}
			open = open[:len(open)-1]
		case ':':
			if !(wordByte(prev) || prev == '[') {
				return bug("a colon outside a range, a slice or a conditional")
			}
		case '?':
			if i == 0 || s[i-1] != ' ' || i+1 == len(s) || s[i+1] != ' ' {
				return bug("a question mark outside a conditional")
			}
		}
		if c >= '0' && c <= '9' && (i == 0 || !wordByte(s[i-1])) {
			j, err := scanNumber(s, i)
			if err != nil {
				return err
			}
			i, prev = j-1, '0'
			continue
		}
		if c == '.' && (i+1 == len(s) || strings.IndexByte("*/^", s[i+1]) < 0) {
			return bug("a point outside a number or an elementwise operator")
		}
		if wordByte(c) && (i == 0 || !wordByte(s[i-1])) {
			j := i
			for j < len(s) && wordByte(s[j]) {
				j++
			}
			word := s[i:j]
			if c == '_' {
				return bug("a name starting with _ in the program")
			}
			if refusedToken[word] {
				return bug("the word %q in the program", word)
			}
			k := j
			for k < len(s) && s[k] == ' ' {
				k++
			}
			switch {
			case prev == '~':
				if !statements[word] || k == len(s) || s[k] != '(' {
					return bug("%q after ~ is not a statement the catalogue lists", word)
				}
			case k < len(s) && s[k] == '(' && word != "for" && !callable[word]:
				return bug("a call outside the catalogue: %q", word)
			}
			i, prev = j-1, 'a'
			continue
		}
		if c != ' ' && c != '\n' {
			prev = c
		}
		if c == '~' {
			// the next token must be a name (checked above when it comes)
			k := i + 1
			for k < len(s) && s[k] == ' ' {
				k++
			}
			if k == len(s) || !(s[k] >= 'a' && s[k] <= 'z') {
				return bug("~ not followed by a statement")
			}
		}
	}
	if len(open) != 0 {
		return bug("unbalanced brackets in the program")
	}
	return nil
}

// scanNumber reads the number at s[i]: digits, [. digits], [e [+-] digits], and nothing of a name
// or another point after it. It returns the offset after the number.
func scanNumber(s string, i int) (int, error) {
	digits := func(j int) int {
		for j < len(s) && s[j] >= '0' && s[j] <= '9' {
			j++
		}
		return j
	}
	j := digits(i)
	if j < len(s) && s[j] == '.' {
		if k := digits(j + 1); k > j+1 {
			j = k
		} else {
			return 0, bug("a number without its decimals")
		}
	}
	if j < len(s) && (s[j] == 'e' || s[j] == 'E') {
		k := j + 1
		if k < len(s) && (s[k] == '+' || s[k] == '-') {
			k++
		}
		if m := digits(k); m > k {
			j = m
		} else {
			return 0, bug("a number without its exponent")
		}
	}
	if j < len(s) && (wordByte(s[j]) || s[j] == '.') {
		return 0, bug("a number followed by a name or a point")
	}
	return j, nil
}

// CheckIdentifiers checks the mapping table against the identifier rule: every identifier is
// lexically Stan's, unique, not a reserved name nor ending with a reserved suffix nor with "__";
// one that starts with a lower-case letter is the person's name of its node, never renamed; one
// that starts with an upper-case letter is generated.
func CheckIdentifiers(mapping []MappingEntry) error {
	seen := map[string]bool{}
	for _, e := range mapping {
		s := e.Stan
		if s == "" || !(s[0] >= 'a' && s[0] <= 'z' || s[0] >= 'A' && s[0] <= 'Z') {
			return bug("the identifier %s is not one", strconv.Quote(s))
		}
		for i := 1; i < len(s); i++ {
			if !wordByte(s[i]) {
				return bug("the identifier %s is not one", strconv.Quote(s))
			}
		}
		if seen[s] {
			return bug("the identifier %s is declared twice", s)
		}
		seen[s] = true
		if strings.HasSuffix(s, "__") || reservedNames[s] || bayescatalog.StanLists().BackendMember(s) != "" {
			return bug("the identifier %s is a word Stan reserves", s)
		}
		for _, suffix := range bayescatalog.StanLists().ReservedSuffixes {
			if strings.HasSuffix(s, suffix) {
				return bug("the identifier %s ends with %s", s, suffix)
			}
		}
		if s[0] >= 'a' && s[0] <= 'z' {
			if e.Name == nil || *e.Name != s || e.Renamed != nil {
				return bug("the identifier %s is not its node's name", s)
			}
		} else if e.Name != nil && e.Renamed == nil {
			return bug("the identifier %s is generated for a name that passes the rule", s)
		}
	}
	return nil
}

// Mismatch is one disagreement between stanc's --info of a program and what the codegen declared
// for it.
type Mismatch struct {
	// Where is the block or list of --info the disagreement is in ("inputs", "parameters",
	// "transformed parameters", "generated quantities", "functions", "distributions").
	Where string
	// Name is the variable, function or distribution; "" when the whole block is concerned.
	Name string
	// What says what differs.
	What string
}

func (m Mismatch) String() string {
	if m.Name == "" {
		return m.Where + ": " + m.What
	}
	return m.Where + " " + m.Name + ": " + m.What
}

// infoVar is one variable of stanc's --info: {"type": "real", "dimensions": 1}.
type infoVar struct {
	Name string
	Type string
	Dims int
}

// infoBlocks are the blocks of --info that list variables, in the order of the CSV's columns.
var infoBlocks = []string{"inputs", "parameters", "transformed parameters", "generated quantities"}

// parseInfo reads stanc's --info (a JSON object: the variables of each block with their type and
// number of dimensions, the functions and the distributions the program uses, the included files),
// keeping the order the variables are declared in.
func parseInfo(info []byte) (vars map[string][]infoVar, lists map[string][]string, err error) {
	dec := json.NewDecoder(bytes.NewReader(info))
	tok, err := dec.Token()
	if err != nil || tok != json.Delim('{') {
		return nil, nil, fmt.Errorf("--info is not a JSON object")
	}
	vars, lists = map[string][]infoVar{}, map[string][]string{}
	for dec.More() {
		kt, err := dec.Token()
		if err != nil {
			return nil, nil, err
		}
		key, _ := kt.(string)
		switch key {
		case "inputs", "parameters", "transformed parameters", "generated quantities":
			if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
				return nil, nil, fmt.Errorf("--info: %q is not an object", key)
			}
			list := []infoVar{}
			for dec.More() {
				nt, err := dec.Token()
				if err != nil {
					return nil, nil, err
				}
				v := struct {
					Type       string `json:"type"`
					Dimensions *int   `json:"dimensions"`
				}{}
				if err := dec.Decode(&v); err != nil {
					return nil, nil, err
				}
				if v.Dimensions == nil {
					return nil, nil, fmt.Errorf("--info: %q has no dimensions", nt)
				}
				list = append(list, infoVar{Name: nt.(string), Type: v.Type, Dims: *v.Dimensions})
			}
			if _, err := dec.Token(); err != nil {
				return nil, nil, err
			}
			vars[key] = list
		case "functions", "distributions", "included_files":
			var l []string
			if err := dec.Decode(&l); err != nil {
				return nil, nil, fmt.Errorf("--info: %q is not a list of names", key)
			}
			lists[key] = l
		default:
			var skip json.RawMessage
			if err := dec.Decode(&skip); err != nil {
				return nil, nil, err
			}
		}
	}
	for _, b := range infoBlocks {
		if _, ok := vars[b]; !ok {
			return nil, nil, fmt.Errorf("--info has no %q", b)
		}
	}
	return vars, lists, nil
}

// dataDims is the number of dimensions of a data variable by its manifest kind; -1 for a kind the
// codegen does not know the shape of (an extension's).
func dataDims(kind string) int {
	switch kind {
	case DataSize, DataMode:
		return 0
	case DataReal, DataInt, DataGroupIndex, DataCategory, DataLift, DataRowsSubset, DataLag, DataFirst, DataNext, DataPrevious:
		return 1
	}
	return -1
}

// CrossCheck compares stanc's --info of a program (the bytes `stanc --info` prints) with the
// program's manifests and the catalogue: the data variables stanc reads, the parameters, the
// transformed parameters and the generated quantities it writes (the names, their order and their
// number of dimensions) are the manifests'; every function and distribution it reports is a name of
// the allow-list. Nothing else of the program is compared: the locals of the model block and of the
// nested blocks (E_<id>_<k>, W_<id>, Full_<id>, Arg<k>_<id>, the loops' variables) are not in
// --info, though they are in the mapping table, and transformed data is not reported at all.
//
// stanc reports a density written as a statement (`y ~ normal(...)`) under its unnormalised name,
// normal_lupdf (normal_lupmf for a mass), where the catalogue lists normal_lpdf: the two are read
// as one. An empty result is a program stanc reads as the codegen meant it.
func CrossCheck(info []byte, p *Program) []Mismatch {
	var out []Mismatch
	add := func(where, name, format string, a ...any) {
		out = append(out, Mismatch{Where: where, Name: name, What: fmt.Sprintf(format, a...)})
	}
	vars, lists, err := parseInfo(info)
	if err != nil {
		return []Mismatch{{Where: "info", What: err.Error()}}
	}
	if p == nil {
		return []Mismatch{{Where: "program", What: "no program"}}
	}
	byStan := map[string]MappingEntry{}
	for _, e := range p.Mapping {
		byStan[e.Stan] = e
	}

	// the data block
	var wantData []string
	for _, d := range p.Data {
		wantData = append(wantData, d.Stan)
	}
	gotData := infoNames(vars["inputs"])
	if !equalStrings(wantData, gotData) {
		add("inputs", "", "stanc reads %v, the data manifest lists %v", gotData, wantData)
	}
	kinds := map[string]string{}
	for _, d := range p.Data {
		kinds[d.Stan] = d.Kind
	}
	for _, v := range vars["inputs"] {
		want := dataDims(kinds[v.Name])
		if e, ok := byStan[v.Name]; ok && e.Kind == KindExtension {
			want = -1
		}
		if want >= 0 && v.Dims != want {
			add("inputs", v.Name, "stanc reads %d dimensions, the manifest's kind %q has %d", v.Dims, kinds[v.Name], want)
		}
	}

	// the written variables: parameters, then transformed parameters, then generated quantities
	blocks := []string{"parameters", "transformed parameters", "generated quantities"}
	want := map[string][]Quantity{}
	var order []string
	for _, q := range p.Quantities {
		e, ok := byStan[q.Stan]
		if !ok {
			add("quantities", q.Stan, "no entry in the mapping table")
			continue
		}
		if e.Block != "parameters" && e.Block != "transformed parameters" && e.Block != "generated quantities" {
			add("quantities", q.Stan, "written from the block %q", e.Block)
			continue
		}
		want[e.Block] = append(want[e.Block], q)
		order = append(order, e.Block)
	}
	for i := 1; i < len(order); i++ {
		if blockRank(order[i]) < blockRank(order[i-1]) {
			add("quantities", "", "the manifest lists a variable of %q after one of %q", order[i], order[i-1])
			break
		}
	}
	for _, b := range blocks {
		var names []string
		for _, q := range want[b] {
			names = append(names, q.Stan)
		}
		if got := infoNames(vars[b]); !equalStrings(names, got) {
			add(b, "", "stanc reports %v, the manifest lists %v", got, names)
			continue
		}
		for i, q := range want[b] {
			if v := vars[b][i]; v.Dims != len(q.Dims) {
				add(b, q.Stan, "stanc reports %d dimensions, the manifest lists %d", v.Dims, len(q.Dims))
			}
		}
	}

	// the functions and distributions stanc reports are the catalogue's
	for _, f := range lists["functions"] {
		if !callable[f] {
			add("functions", f, "not a function the catalogue allows")
		}
	}
	for _, d := range lists["distributions"] {
		n := densityName(d)
		if !callable[n] {
			add("distributions", d, "not a distribution the catalogue allows (%s)", n)
		}
	}
	if len(lists["included_files"]) != 0 {
		add("included_files", "", "the program includes %v", lists["included_files"])
	}
	return out
}

// densityName is the catalogue's name of a density stanc reports: normal_lupdf is normal_lpdf and
// binomial_logit_lupmf binomial_logit_lpmf.
func densityName(n string) string {
	switch {
	case strings.HasSuffix(n, "_lupdf"):
		return strings.TrimSuffix(n, "_lupdf") + "_lpdf"
	case strings.HasSuffix(n, "_lupmf"):
		return strings.TrimSuffix(n, "_lupmf") + "_lpmf"
	}
	return n
}

func blockRank(b string) int {
	switch b {
	case "parameters":
		return 0
	case "transformed parameters":
		return 1
	}
	return 2
}

func infoNames(vs []infoVar) []string {
	out := []string{}
	for _, v := range vs {
		out = append(out, v.Name)
	}
	return out
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
