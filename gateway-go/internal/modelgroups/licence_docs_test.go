package modelgroups

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// The documentation quotes the clause of the weights' licence word for word and carries the licence's sha256 in a comment. When a
// release moves the pinned revision and the licence's text changes, the recipe's sha256 changes: this test fails until the
// documentation quotes the new clause (docs/specs/foundation-forecast.md, «When the licence changes»).
func TestTheDocumentationQuotesTheClauseOfTheWeightsLicenceWithItsSHA256(t *testing.T) {
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	var doc []byte
	for d := dir; ; d = filepath.Dir(d) {
		if b, err := os.ReadFile(filepath.Join(d, "docs", "capabilities", "models.md")); err == nil {
			doc = b
			break
		}
		if filepath.Dir(d) == d {
			t.Skip("the documentation is not beside the gateway here")
		}
	}
	// the quote sits in a block quote: take the «> » off each line, and compare the words
	quote := regexp.MustCompile(`(?m)^> ?`)
	words := func(s string) string { return strings.Join(strings.Fields(s), " ") }
	text := words(quote.ReplaceAllString(string(doc), ""))
	checked := 0
	for _, g := range Groups() {
		if g.Recipe.Weights == nil {
			continue
		}
		checked++
		l := g.Recipe.Weights.Licence
		if want := "<!-- licence-sha256: " + l.SHA256 + " -->"; !strings.Contains(string(doc), want) {
			t.Errorf("%s: docs/capabilities/models.md does not carry %q: the licence changed, so the clause quoted there must be read again", g.ID, want)
		}
		full := words(g.LicenceText())
		start := strings.Index(full, "2. You are not authorized")
		end := strings.Index(full, "https://mistral.ai/.")
		if start < 0 || end < start {
			t.Fatalf("%s: the licence's second point is not where this test looks for it", g.ID)
		}
		clause := full[start : end+len("https://mistral.ai/.")]
		if !strings.Contains(text, clause) {
			t.Errorf("%s: docs/capabilities/models.md does not quote the clause of the licence word for word:\n%s", g.ID, clause)
		}
	}
	if checked == 0 {
		t.Fatal("no group with weights")
	}
}
