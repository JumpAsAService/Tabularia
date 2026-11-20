package modelcatalog

import (
	"fmt"
	"strings"
	"testing"
)

func TestPredictsOptionsAreTheModels(t *testing.T) {
	got, err := CheckPredict("cox", "default", map[string]any{"at_times": []any{24.0, 12.0, 6.5}, "interval": nil})
	if err != nil || fmt.Sprint(got) != "map[at_times:[6.5 12 24]]" {
		t.Fatalf("%v %v", got, err)
	}
	if got, err := CheckPredict("linear", "unpenalized", map[string]any{"interval": false, "horizon": nil}); err != nil || len(got) != 0 {
		t.Errorf("what is off or empty is not given: %v %v", got, err)
	}
	if got, err := CheckPredict("arima", "manual", map[string]any{"horizon": 12.0, "interval": true}); err != nil || fmt.Sprint(got) != "map[horizon:12 interval:true]" {
		t.Errorf("arima: %v %v", got, err)
	}
	// a threshold is a binary boosting model's, not a regression's or a multiclass one's
	if got, err := CheckPredict("xgboost", "binary", map[string]any{"threshold": 0.3}); err != nil || fmt.Sprint(got) != "map[threshold:0.3]" {
		t.Errorf("xgboost:binary: %v %v", got, err)
	}
	if _, err := CheckPredict("catboost", "multiclass", map[string]any{"threshold": 0.3}); err == nil ||
		!strings.HasSuffix(err.Error(), "not to catboost (multiclass)") {
		t.Errorf("catboost:multiclass: %v", err)
	}
	for _, c := range []struct {
		model  string
		params map[string]any
		want   string
	}{
		{"linear", map[string]any{"threshold": 0.4}, "Predict's option 'threshold' applies to logistic, xgboost (binary), catboost (binary), not to linear"},
		{"linear", map[string]any{"colour": "red"}, "Predict has no option 'colour' (it takes: at_times, horizon, interval, threshold)"},
		{"logistic", map[string]any{"threshold": 1.5}, "Predict: 'threshold'"},
		{"arima", map[string]any{"horizon": 5000.0}, "Predict: 'horizon'"},
		{"cox", map[string]any{"at_times": []any{3.0, 3.0}}, "holds a number twice"},
		{"nothing", map[string]any{}, "there is no model 'nothing'"},
	} {
		if _, err := CheckPredict(c.model, "", c.params); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s %v: %v, want %q", c.model, c.params, err, c.want)
		}
	}
}

func TestTheColumnsAPredictAdds(t *testing.T) {
	for _, c := range []struct {
		model  string
		params map[string]any
		want   string
	}{
		{"linear", nil, "prediction"},
		{"linear", map[string]any{"interval": true}, "prediction prediction_low prediction_high"},
		{"logistic", map[string]any{"threshold": 0.3}, "prediction prediction_class"},
		{"cox", map[string]any{"at_times": []any{3.0, 6.5}}, "prediction survival_at_3 survival_at_6.5"},
		{"kaplan_meier", map[string]any{"interval": true}, "prediction prediction_low prediction_high"},
		{"arima", map[string]any{"interval": true, "horizon": 6.0}, "prediction lower_80 upper_80 lower upper"},
		{"nothing", nil, ""},
	} {
		if got := strings.Join(PredictColumns(c.model, "", c.params), " "); got != c.want {
			t.Errorf("%s %v: %q, want %q", c.model, c.params, got, c.want)
		}
	}
	// the boosting models: by the card's variant
	for _, c := range []struct{ model, variant, want string }{
		{"xgboost", "regression", "prediction"},
		{"catboost", "binary", "prediction prediction_class"},
		{"xgboost", "multiclass", "prediction prediction_probability prediction_prob_*"},
	} {
		if got := strings.Join(PredictColumns(c.model, c.variant, nil), " "); got != c.want {
			t.Errorf("%s:%s: %q, want %q", c.model, c.variant, got, c.want)
		}
	}
	if !HasVariant("linear", "penalized") || HasVariant("linear", "bayesian") || HasVariant("nothing", "default") {
		t.Error("HasVariant")
	}
}
