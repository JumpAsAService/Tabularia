package api

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
)

// the expectations are Python's (services/masking.py, statistics, routes/performance.py)
func TestMaskEmail(t *testing.T) {
	for in, want := range map[string]string{
		"alice.rossi@esempio.it": "a***@esempio.it", "@x.it": "***@x.it", "nobody": "***", "": "", "  ": "",
		"é@x.test": "é***@x.test", "a@b@c": "a***@c",
	} {
		if got := maskEmail(in); got != want {
			t.Errorf("maskEmail(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestMedianAndPercentile(t *testing.T) {
	if median(nil) != nil || pct(nil, 0.95) != nil {
		t.Fatal("empty lists give None")
	}
	if got := median([]float64{3, 1, 2, 10}); got != 2.5 {
		t.Errorf("median = %v", got)
	}
	// round(0.95 * 2) = round(1.9) = 2; round(0.95 * 10) = round(9.5) = 10 (half to even)
	if got := pct([]float64{1, 2, 3}, 0.95); got != 3.0 {
		t.Errorf("pct = %v", got)
	}
	vals := make([]float64, 11)
	for i := range vals {
		vals[i] = float64(i)
	}
	if got := pct(vals, 0.95); got != 10.0 {
		t.Errorf("pct of 11 = %v", got)
	}
}

func TestPyEqual(t *testing.T) {
	a, _ := pyd.LoadsStr(`{"rules": [1, true, 2.0], "b": {"x": null}}`)
	b, _ := pyd.LoadsStr(`{"b": {"x": null}, "rules": [1.0, 1, 2]}`)
	c, _ := pyd.LoadsStr(`{"rules": []}`)
	if !pyEqual(a, b) || pyEqual(a, c) || pyEqual(pyd.NewObject(), c) {
		t.Error("dicts compare as Python's ==")
	}
}

func TestEngineSettingValue(t *testing.T) {
	text := func(s string) *string { return &s }
	for _, c := range []struct {
		row  engineSettingRow
		want any
	}{
		{engineSettingRow{Value: 5}, int64(5)},
		{engineSettingRow{Value: 5, ValueJSON: text("5.5")}, int64(5)}, // round(5.5) = 6: the integer wins
		{engineSettingRow{Value: 6, ValueJSON: text("5.5")}, 5.5},
		{engineSettingRow{Value: 1, ValueJSON: text("true")}, true},
		{engineSettingRow{Value: 1, ValueJSON: text("nope")}, nil},
	} {
		if got, _ := engineSettingValue(c.row); got != c.want {
			t.Errorf("%+v: %v, want %v", c.row, got, c.want)
		}
	}
}
