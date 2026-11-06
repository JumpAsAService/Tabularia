package api

import "testing"

func TestShellQuoteIsShlexQuote(t *testing.T) {
	for in, want := range map[string]string{"": "''", "registry.test:5000": "registry.test:5000",
		"type=image,name=r/x:v1,push=true": "type=image,name=r/x:v1,push=true", "a b": "'a b'", "it's": `'it'"'"'s'`} {
		if got := sbShellQuote(in); got != want {
			t.Errorf("%q: %q, want %q", in, got, want)
		}
	}
}

func TestPackagesAreParsePackages(t *testing.T) {
	got, err := sbParsePackages("python", "polars==1.0\n# x\npandas>=2,<3  # pinned\n\n")
	if err != nil || len(got) != 2 || got[1] != "pandas>=2,<3" {
		t.Errorf("%v %v", got, err)
	}
	if _, err := sbParsePackages("r", "data.table\nrm -rf /"); err == nil || err.Error() != "line 2: «rm -rf /» is not a package" {
		t.Errorf("%v", err)
	}
}
