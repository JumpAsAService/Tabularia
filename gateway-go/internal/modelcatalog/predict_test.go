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
	// a parametric survival model takes the quantiles, the times and the interval, and is predicted beyond the data
	if got, err := CheckPredict("parametric_survival", "ph_aft", map[string]any{"quantiles": []any{0.9, 0.1}, "at_times": []any{30.0}, "interval": true}); err != nil ||
		fmt.Sprint(got) != "map[at_times:[30] interval:true quantiles:[0.1 0.9]]" {
		t.Errorf("parametric_survival: %v %v", got, err)
	}
	if _, err := CheckPredict("cox", "default", map[string]any{"quantiles": []any{0.5}}); err == nil ||
		!strings.Contains(err.Error(), "Predict's option 'quantiles' applies to parametric_survival, not to cox") {
		t.Errorf("quantiles are a parametric model's: %v", err)
	}
	if _, err := CheckPredict("parametric_survival", "aft", map[string]any{"quantiles": []any{1.0}}); err == nil ||
		!strings.Contains(err.Error(), "Predict: 'quantiles'") {
		t.Errorf("a quantile of 1: %v", err)
	}
	// a customer lifetime model takes a horizon (in the card's unit) and nothing else
	if got, err := CheckPredict("clv", "value", map[string]any{"horizon": 26.0}); err != nil || fmt.Sprint(got) != "map[horizon:26]" {
		t.Errorf("clv: %v %v", got, err)
	}
	if _, err := CheckPredict("bg_bb", "purchases", map[string]any{"interval": true}); err == nil ||
		!strings.Contains(err.Error(), "Predict's option 'interval' applies to") || !strings.Contains(err.Error(), "not to bg_bb") {
		t.Errorf("interval is not a lifetime model's: %v", err)
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
		{"linear", map[string]any{"colour": "red"}, "Predict has no option 'colour' (it takes: at_times, horizon, interval, quantiles, threshold)"},
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
		{"parametric_survival", nil, "prediction"},
		{"parametric_survival", map[string]any{"quantiles": []any{0.25, 0.75}, "at_times": []any{30.0}, "interval": true},
			"prediction survival_at_30 prediction_low prediction_high quantile_0.25 quantile_0.75"},
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
		// a customer lifetime model's: by the card's variant, with the spend part or without it
		{"clv", "purchases", "prediction prediction_p_alive"},
		{"clv", "value", "prediction prediction_p_alive prediction_spend prediction_clv"},
		{"bg_bb", "value", "prediction prediction_p_alive prediction_spend prediction_clv"},
	} {
		if got := strings.Join(PredictColumns(c.model, c.variant, nil), " "); got != c.want {
			t.Errorf("%s:%s: %q, want %q", c.model, c.variant, got, c.want)
		}
	}
	if !HasVariant("linear", "penalized") || HasVariant("linear", "bayesian") || HasVariant("nothing", "default") {
		t.Error("HasVariant")
	}
}
