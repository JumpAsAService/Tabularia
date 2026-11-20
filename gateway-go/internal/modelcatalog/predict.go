package modelcatalog

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// The Predict node's options (the catalogue's `predict`: takes, defaults, applies_to, columns):
// what a step that applies a model takes besides the model, and the columns it adds
// (docs/specs/statistical-models.md, «Predict» → «What it adds»).

type predictSpec struct {
	Takes     map[string]string              `json:"takes"`
	Defaults  map[string]any                 `json:"defaults"`
	AppliesTo map[string][]string            `json:"applies_to"`
	Columns   map[string]map[string][]string `json:"columns"`
}

var predictData = func() predictSpec {
	var p predictSpec
	if err := json.Unmarshal(data.predict, &p); err != nil {
		panic("modelcatalog: catalog.json predict: " + err.Error())
	}
	for key, kind := range p.Takes {
		if _, ok := data.kinds[kind]; !ok {
			panic(fmt.Sprintf("modelcatalog: catalog.json predict.takes.%s: unknown kind %q", key, kind))
		}
	}
	return p
}()

// HasVariant says whether the model has that variant.
func HasVariant(model, variant string) bool {
	m, ok := data.byID[model]
	if !ok {
		return false
	}
	for _, v := range m.Variants {
		if v.ID == variant {
			return true
		}
	}
	return false
}

// appliesTo says whether a list of `applies_to` names the model, or the model's variant as
// `model:variant` (the boosting models' threshold is the binary variant's).
func appliesTo(list []string, model, variant string) bool {
	return contains(list, model) || variant != "" && contains(list, model+":"+variant)
}

// CheckPredict reads a Predict node's options for the model (and the variant) its version
// holds: a key Predict does not take is refused, as one that does not apply to that model; a
// value is read by its kind. The answer holds only what was given (a null, an empty list or
// false is not given): the runner's defaults are the card's.
func CheckPredict(model, variant string, params map[string]any) (map[string]any, error) {
	if _, ok := data.byID[model]; !ok {
		return nil, &Error{Message: fmt.Sprintf("there is no model '%s' (the models: %s)", model, strings.Join(Models(), ", "))}
	}
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := map[string]any{}
	for _, k := range keys {
		v := params[k]
		kindName, takes := predictData.Takes[k]
		if !takes {
			all := make([]string, 0, len(predictData.Takes))
			for t := range predictData.Takes {
				all = append(all, t)
			}
			sort.Strings(all)
			return nil, &Error{Message: fmt.Sprintf("Predict has no option '%s' (it takes: %s)", k, strings.Join(all, ", "))}
		}
		if v == nil || v == false {
			continue
		}
		if list, isList := v.([]any); isList && len(list) == 0 {
			continue
		}
		if !appliesTo(predictData.AppliesTo[k], model, variant) {
			what := model
			if variant != "" && strings.Contains(strings.Join(predictData.AppliesTo[k], ","), model+":") {
				what = model + ":" + variant
			}
			names := make([]string, len(predictData.AppliesTo[k]))
			for i, m := range predictData.AppliesTo[k] {
				names[i] = modelVariantName(m)
			}
			return nil, &Error{Message: fmt.Sprintf("Predict's option '%s' applies to %s, not to %s", k,
				strings.Join(names, ", "), modelVariantName(what))}
		}
		read, err := readValue("Predict", k, data.kinds[kindName], v)
		if err != nil {
			return nil, err
		}
		if read == false {
			continue
		}
		out[k] = read
	}
	return out, nil
}

// modelVariantName is how a sentence names an entry of `applies_to`: «logistic», «xgboost (binary)».
func modelVariantName(entry string) string {
	if m, v, isVariant := strings.Cut(entry, ":"); isVariant {
		return m + " (" + v + ")"
	}
	return entry
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// PredictColumns are the columns a Predict step adds for the model (its variant's, when the
// catalogue keys them `model:variant`) with its checked options, in the catalogue's order:
// `survival_at_<t>` once per time, t written as the runner writes it. A name ending in «*»
// whose option is not a list of times (`prediction_prob_*`: one per class of the card) is
// written as it is: the card's classes are not the catalogue's. A `*` inside a name
// (`quantile_*_low`) is filled the same way, under a key `a+b` given when both options are.
func PredictColumns(model, variant string, checked map[string]any) []string {
	spec := predictData.Columns[model+":"+variant]
	if spec == nil {
		spec = predictData.Columns[model]
	}
	if spec == nil {
		return nil
	}
	out := append([]string(nil), spec["always"]...)
	options := make([]string, 0, len(spec))
	for k := range spec {
		if k != "always" {
			options = append(options, k)
		}
	}
	sort.Strings(options)
	for _, option := range options {
		// a key `a+b` is given when both options are; its `*` is one per entry of the first (the
		// interval of each quantile, of each time)
		given := true
		for _, part := range strings.Split(option, "+") {
			if _, has := checked[part]; !has {
				given = false
			}
		}
		if !given {
			continue
		}
		v := checked[strings.SplitN(option, "+", 2)[0]]
		for _, name := range spec[option] {
			prefix, suffix, isPattern := strings.Cut(name, "*")
			if !isPattern {
				out = append(out, name)
				continue
			}
			list, _ := v.([]any)
			seen := map[string]bool{}
			for _, t := range list {
				if f, isNum := t.(float64); isNum {
					label := timeLabel(f)
					if !seen[label] {
						seen[label] = true
						out = append(out, prefix+label+suffix)
					}
				}
			}
		}
	}
	return out
}

// timeLabel is km_time_label of the survival runner for the numbers a form gives: a whole
// number without decimals, else its shortest form.
func timeLabel(t float64) string {
	if t == float64(int64(t)) && t < 1e15 && t > -1e15 {
		return strconv.FormatInt(int64(t), 10)
	}
	return strconv.FormatFloat(t, 'g', -1, 64)
}
