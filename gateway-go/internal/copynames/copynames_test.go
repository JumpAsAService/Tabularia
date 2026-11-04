package copynames

import "testing"

func TestBaseName(t *testing.T) {
	for in, want := range map[string]string{
		"Sales": "Sales", "Sales (1)": "Sales", "Sales (1) (2)": "Sales (1)", "Sales(1)": "Sales(1)",
		"Sales  (1)": "Sales  (1)", " (1)": " (1)", "a\nb (1)": "a\nb (1)", "x (٣)": "x", "x ()": "x ()",
		"x (1a)": "x (1a)", "(1)": "(1)",
	} {
		if got := BaseName(in); got != want {
			t.Errorf("BaseName(%q) = %q, want %q", in, got, want)
		}
	}
	if got := CopyName("Sales (1)", map[string]bool{"Sales (1)": true, "Sales (2)": true}); got != "Sales (3)" {
		t.Error(got)
	}
}
