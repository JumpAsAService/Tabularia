package bayescodegen

import (
	"errors"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

const (
	diffNode = `{"id":"d","kind":"transformed","name":"d","label":"D","plate":"rows","value":{"expr":"y - lag(y, 1)"}}`
	levelRW  = `{"id":"lv","kind":"state","name":"lv","label":"Level","plate":"rows","process":"random_walk","init":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}},"scale":{"expr":"s"}}`
	lagOfLv  = `{"id":"q","kind":"transformed","name":"q","label":"Q","plate":"rows","value":{"expr":"lag(lv, 1) * a"}}`
)

func stateWith(form, init string) string {
	return `{"id":"lv2","kind":"state","name":"lv2","label":"Level 2","plate":"rows","process":"random_walk",` + form +
		`"init":` + init + `,"scale":{"expr":"s"}}`
}

func likOf(mu string) string {
	return `{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"` + mu + `"},"sigma":{"expr":"s"}}}`
}

// The cases the generator cannot write (the lag of a value computed with lag, an init that reads a
// lagged value, a centred state whose init is not on the real line) are the validator's: an error of
// the graph and ErrInvalidModel from Generate, never ErrUnsupported (lot S2's correction).
func TestValidatorRefusesWhatLagAndInitDoNotWrite(t *testing.T) {
	gamma := `{"dist":"gamma","args":{"alpha":{"expr":"2"},"beta":{"expr":"1"}}}`
	cases := map[string]struct {
		data   []byte
		code   string
		reason string
	}{
		"the lag of a value that uses lag": {seriesJSON(false, paramA, paramS, diffNode, likOf("a + lag(d, 1)")),
			"graph.lag_nested", "nested"},
		"an init that reads a lagged value": {seriesJSON(false, paramA, paramS, levelRW, lagOfLv,
			stateWith("", `{"dist":"normal","args":{"mu":{"expr":"q"},"sigma":{"expr":"1"}}}`), likOf("lv + lv2")),
			"graph.lag_nested", "init"},
		"a centred state with a gamma init": {seriesJSON(false, paramA, paramS,
			stateWith(`"form":"centred",`, gamma), likOf("lv2")),
			"graph.state", "init_support"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			g, findings := bayesgraph.Decode(c.data)
			if g == nil {
				t.Fatalf("does not decode: %v", findings)
			}
			m := bayesgraph.BuildModel(g)
			found := false
			for _, f := range m.Findings {
				if f.Code == c.code && f.Severity == bayesgraph.Error && f.Args["reason"] == c.reason {
					found = true
				}
			}
			if !found {
				t.Fatalf("no %s (%s) error: %+v", c.code, c.reason, m.Findings)
			}
			if _, err := Generate(m, nil); !errors.Is(err, ErrInvalidModel) || errors.Is(err, ErrUnsupported) {
				t.Fatalf("want ErrInvalidModel, got %v", err)
			}
		})
	}
	// beside: what the validator accepts the generator writes
	for name, data := range map[string][]byte{
		"a lag of a value without lag": seriesJSON(false, paramA, paramS,
			`{"id":"d","kind":"transformed","name":"d","label":"D","plate":"rows","value":{"expr":"2 * y"}}`, likOf("a + lag(d, 1)")),
		"an init that reads a value without lag": seriesJSON(false, paramA, paramS, levelRW, lagOfLv,
			stateWith("", `{"dist":"normal","args":{"mu":{"expr":"a"},"sigma":{"expr":"1"}}}`), likOf("lv + lv2 + q")),
		"a centred state with a normal init": seriesJSON(false, paramA, paramS,
			stateWith(`"form":"centred",`, `{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}`), likOf("lv2")),
		"a non-centred state with a gamma init": seriesJSON(false, paramA, paramS,
			stateWith(`"form":"non_centred",`, gamma), likOf("lv2")),
	} {
		t.Run(name, func(t *testing.T) {
			_, m := modelOf(t, data)
			if _, err := Generate(m, nil); errors.Is(err, ErrUnsupported) {
				t.Fatalf("the validator accepts it and the generator does not write it: %v", err)
			}
		})
	}
}
