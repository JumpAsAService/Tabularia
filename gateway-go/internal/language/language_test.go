package language

import "testing"

// the cases of gateway/tests for core/language.py
func TestOf(t *testing.T) {
	for in, want := range map[string]string{
		"": "en", "it": "it", "it-IT,it;q=0.9,en;q=0.8": "it", "fr-CH": "fr", "pt-BR,de;q=0.5": "de",
		"PT, ES": "es", "zh": "en", " de-AT ; q=1": "de",
	} {
		if got := Of(in); got != want {
			t.Errorf("Of(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestPickFallsBackToEnglish(t *testing.T) {
	table := map[string]string{"en": "E", "it": "I"}
	if Pick(table, "it-IT") != "I" || Pick(table, "de") != "E" {
		t.Error(Pick(table, "it-IT"), Pick(table, "de"))
	}
}
