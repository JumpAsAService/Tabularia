package flowaccess

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// services/container_nodes.py and the names of services/container_registry.py; Python's `$`
// matches before a final line feed too: `\n?\z` here.
const (
	maxItems      = 100
	maxItemChars  = 4000
	maxVariables  = 100
	maxValueChars = 20000
	defaultCPU    = "1"
	defaultMemory = "1Gi"
)

var (
	variableName = regexp.MustCompile(`^[A-Z_][A-Z0-9_]*\n?\z`)
	cpuRe        = regexp.MustCompile(`^(\d+(?:\.\d+)?)(m?)\n?\z`)
	memoryRe     = regexp.MustCompile(`^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?\n?\z`)
	repositoryRe = regexp.MustCompile(`^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*\n?\z`)
	tagRe        = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\n?\z`)
	digestRe     = regexp.MustCompile(`^sha256:[0-9a-f]{64}\n?\z`)
	bytesOf      = map[string]float64{"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
		"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40}
	reservedPrefixes = []string{"TABULARIA_", "AWS_"}
)

// ContainerNodes is container_nodes.container_nodes.
func ContainerNodes(definition any) []pyjson.Object {
	var parsed pyjson.Object
	switch d := definition.(type) {
	case pyjson.Object:
		parsed = d
	case string:
		parsed = pyjson.Obj(parse(d, "{}"))
	}
	nodes, ok := parsed.Value("nodes").([]any)
	if !ok {
		return nil
	}
	var out []pyjson.Object
	for _, n := range nodes {
		if node := pyjson.Obj(n); node != nil && node.Value("type") == "container" {
			out = append(out, node)
		}
	}
	return out
}

func containerLabel(node pyjson.Object) string {
	return pyjson.Str(pyjson.Or(pyjson.Or(data(node).Value("label"), node.Value("id")), "Container"))
}

func containerRefuse(node pyjson.Object, message string) *auth.Refusal {
	return refuse(422, fmt.Sprintf("Container «%s»: %s", containerLabel(node), message))
}

func stringsOf(v any) []any {
	list, ok := v.([]any)
	if !ok {
		return []any{}
	}
	out := make([]any, len(list))
	for i, x := range list {
		out[i] = pyjson.Str(x)
	}
	return out
}

// variablesOf is container_nodes.variables_of, in the order a dict keeps.
func variablesOf(node pyjson.Object) pyjson.Object {
	out := pyjson.Object{}
	list, _ := pyjson.Or(data(node).Value("variables"), []any{}).([]any)
	for _, v := range list {
		o := pyjson.Obj(v)
		if o != nil && pyjson.Truthy(o.Value("name")) {
			out = out.Set(pyjson.Str(o.Value("name")), pyjson.Str(pyjson.Or(o.Value("value"), "")))
		}
	}
	return out
}

// ContainerFingerprint is container_nodes.fingerprint.
func ContainerFingerprint(node pyjson.Object) string {
	d := data(node)
	vars := variablesOf(node)
	sort.SliceStable(vars, func(i, j int) bool { return vars[i].Key < vars[j].Key })
	pairs := make([]any, len(vars))
	for i, kv := range vars {
		pairs[i] = []any{kv.Key, kv.Value}
	}
	s, _ := pyjson.Dumps(pyjson.Object{
		{Key: "registry", Value: d.Value("registryId")}, {Key: "repository", Value: d.Value("repository")},
		{Key: "tag", Value: d.Value("tag")}, {Key: "command", Value: stringsOf(d.Value("command"))},
		{Key: "args", Value: stringsOf(d.Value("args"))}, {Key: "variables", Value: pairs},
	}, pyjson.Options{EnsureASCII: true, SortKeys: true})
	return sha(s)
}

func cores(cpu string) float64 {
	m := cpuRe.FindStringSubmatch(cpu)
	f, _ := strconv.ParseFloat(m[1], 64)
	if m[2] != "" {
		return f / 1000
	}
	return f
}

func memoryBytes(memory string) float64 {
	m := memoryRe.FindStringSubmatch(memory)
	f, _ := strconv.ParseFloat(m[1], 64)
	return f * bytesOf[m[2]]
}

// checkContainer is container_nodes.check.
func checkContainer(node pyjson.Object, complete bool, s Settings) *auth.Refusal {
	d := data(node)
	repository := pyStrip(pyjson.Str(pyjson.Or(d.Value("repository"), "")))
	tag := pyStrip(pyjson.Str(pyjson.Or(d.Value("tag"), "")))
	if _, isInt := pyjson.IsInt(d.Value("registryId")); complete && !(isInt && repository != "") {
		return containerRefuse(node, "choose its registry and its image")
	}
	if repository != "" && !repositoryRe.MatchString(repository) {
		return containerRefuse(node, fmt.Sprintf("«%s» is not a repository name", repository))
	}
	if tag != "" && !(tagRe.MatchString(tag) || digestRe.MatchString(tag)) {
		return containerRefuse(node, fmt.Sprintf("«%s» is not a tag (letters, digits, . _ -) or a digest (sha256:…)", tag))
	}
	for _, kw := range [][2]string{{"command", "the command"}, {"args", "the arguments"}} {
		value, present := d.Get(kw[0])
		if !present {
			value = []any{}
		}
		list, ok := value.([]any)
		texts := ok
		for _, x := range list {
			if _, isText := x.(string); !isText {
				texts = false
			}
		}
		if !texts {
			return containerRefuse(node, kw[1]+" are a list of texts")
		}
		long := len(list) > maxItems
		for _, x := range list {
			long = long || utf8.RuneCountInString(x.(string)) > maxItemChars
		}
		if long {
			return containerRefuse(node, fmt.Sprintf("%s: at most %d items of %d characters", kw[1], maxItems, maxItemChars))
		}
	}
	variables, present := d.Get("variables")
	if !present {
		variables = []any{}
	}
	vars, ok := variables.([]any)
	if !ok || len(vars) > maxVariables {
		return containerRefuse(node, fmt.Sprintf("at most %d variables", maxVariables))
	}
	seen := map[string]bool{}
	for _, v := range vars {
		o := pyjson.Obj(v)
		name := ""
		if o != nil {
			name = pyjson.Str(pyjson.Or(o.Value("name"), ""))
		}
		if !variableName.MatchString(name) || utf8.RuneCountInString(name) > 128 {
			return containerRefuse(node, fmt.Sprintf("«%s» is not a variable name: capitals, digits and _, not starting with a digit", name))
		}
		for _, p := range reservedPrefixes {
			if strings.HasPrefix(name, p) {
				return containerRefuse(node, fmt.Sprintf("%s: names starting with %s are set by Tabularia", name, strings.Join(reservedPrefixes, " or ")))
			}
		}
		if seen[name] {
			return containerRefuse(node, fmt.Sprintf("the variable %s is there twice", name))
		}
		seen[name] = true
		value, has := o.Get("value")
		if !has {
			value = ""
		}
		if _, isText := value.(string); !isText || utf8.RuneCountInString(pyjson.Str(pyjson.Or(value, ""))) > maxValueChars {
			return containerRefuse(node, fmt.Sprintf("the value of %s is a text of at most %d characters", name, maxValueChars))
		}
	}
	cpu := pyStrip(pyjson.Str(pyjson.Or(d.Value("cpu"), defaultCPU)))
	memory := pyStrip(pyjson.Str(pyjson.Or(d.Value("memory"), defaultMemory)))
	if !cpuRe.MatchString(cpu) || cores(cpu) <= 0 {
		return containerRefuse(node, fmt.Sprintf("«%s» is not a CPU quantity: 1, 0.5, 500m", cpu))
	}
	if cpuRe.MatchString(s.MaxCPU) && cores(cpu) > cores(s.MaxCPU) {
		return containerRefuse(node, fmt.Sprintf("at most %s CPU on this installation", s.MaxCPU))
	}
	if !memoryRe.MatchString(memory) || memoryBytes(memory) < 1<<20 {
		return containerRefuse(node, fmt.Sprintf("«%s» is not a memory quantity: 512Mi, 2Gi", memory))
	}
	if memoryRe.MatchString(s.MaxMemory) && memoryBytes(memory) > memoryBytes(s.MaxMemory) {
		return containerRefuse(node, fmt.Sprintf("at most %s of memory on this installation", s.MaxMemory))
	}
	get := func(k string, def int64) any {
		if v, ok := d.Get(k); ok {
			return v
		}
		return def
	}
	timeout, okT := pyIntValue(get("timeoutSeconds", 900))
	retries, okR := pyIntValue(get("retries", 0))
	delay, okD := pyIntValue(get("retryDelaySeconds", 60))
	if !okT || timeout < 10 || timeout > s.MaxTimeoutSeconds {
		return containerRefuse(node, fmt.Sprintf("the timeout is a number of seconds between 10 and %d", s.MaxTimeoutSeconds))
	}
	if !okR || retries < 0 || retries > maxRetries {
		return containerRefuse(node, fmt.Sprintf("the retries are between 0 and %d", maxRetries))
	}
	if !okD || delay < 0 || delay > 3600 {
		return containerRefuse(node, "the delay between retries is between 0 and 3600 seconds")
	}
	return nil
}

// checkContainerDefinition is container_nodes.check_definition.
func checkContainerDefinition(definition string, s Settings) *auth.Refusal {
	for _, node := range ContainerNodes(definition) {
		if r := checkContainer(node, false, s); r != nil {
			return r
		}
	}
	return nil
}

// CheckContainer is container_nodes.check, for a flow group's container cell.
func CheckContainer(node pyjson.Object, complete bool, s Settings) *auth.Refusal {
	return checkContainer(node, complete, s)
}

// ContainerLabel is container_nodes.label.
func ContainerLabel(node pyjson.Object) string { return containerLabel(node) }

// RegistryByID reads a container registry, nil when it does not exist.
func (a *Access) RegistryByID(ctx context.Context, id int64) (*Registry, error) {
	return a.registry(ctx, id)
}

// RegistryBlocked is registry_access.blocked.
func (a *Access) RegistryBlocked(ctx context.Context, definition string) (string, error) {
	return a.registryBlocked(ctx, definition)
}

// VariablesOf is container_nodes.variables_of, in the order a dict keeps.
func VariablesOf(node pyjson.Object) pyjson.Object { return variablesOf(node) }

// StringsOf is container_nodes.strings.
func StringsOf(v any) []any { return stringsOf(v) }
