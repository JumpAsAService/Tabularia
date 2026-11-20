package modelgroups

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestAPredictTask(t *testing.T) {
	task, err := PredictTask("arima", "manual", map[string]any{"horizon": 6.0, "interval": true})
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(task.JSON(), &got); err != nil {
		t.Fatal(err)
	}
	if got["mode"] != "predict" || got["group"] != "timeseries" || got["output"] != "forecast" || got["write_output"] != true ||
		got["mlflow"] != false || got["runner_hash"] == "" {
		t.Errorf("%s", task.JSON())
	}
	if task, _ := PredictTask("logistic", "penalized", nil); task.Output != "rows" || len(task.Params) != 0 {
		t.Errorf("%+v", task)
	}
	if strings.Contains(string(task.JSON()), "MODEL_IN") {
		t.Error("the card is never in task.json")
	}
	for _, c := range [][2]string{{"linear", "bayesian"}, {"nothing", "default"}} {
		if _, err := PredictTask(c[0], c[1], nil); err == nil {
			t.Errorf("%v: no refusal", c)
		}
	}
}
