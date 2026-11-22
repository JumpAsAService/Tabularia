package api

import (
	"strings"
	"testing"
)

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

// A push denied after network errors says that the credentials were refused or expired during the slow connection, and to
// check first that the build reaches the registry (lot F9: on kind a push that timed out for two minutes ended in a 401 when
// BuildKit's token had expired, and the panel blamed the credentials alone). A 401 alone keeps its sentence.
func TestAPushDeniedAfterNetworkErrorsSaysBothCauses(t *testing.T) {
	slow := "#12 exporting to image\n#12 pushing layers\n#12 ERROR: failed to do request: Head \"https://10.89.0.4:5000/v2/x/blobs/sha256:ab\": " +
		"dial tcp 10.89.0.4:5000: i/o timeout\n------\nERROR: failed to solve: failed to push 10.89.0.4:5000/x:v2: " +
		"failed to authorize: failed to fetch oauth token: unexpected status: 401 Unauthorized\n"
	got := sbPushDeniedSentence(slow)
	for _, want := range []string{"«401 Unauthorized»", "«i/o timeout»", "were refused, or they expired", "slow or interrupted",
		"networkPolicy.userCodePrivateCIDRs", "scripts.registry.pushSecret"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in %q", want, got)
		}
	}
	if !sbPushDenied.MatchString(slow) {
		t.Error("the slow push is not a denied push")
	}
	plain := "ERROR: failed to solve: failed to push r/x:v1: unexpected status from PUT request: 401 Unauthorized\n"
	if got := sbPushDeniedSentence(plain); got != "The image was built but not pushed: the registry refused the credentials. Check the "+
		"Secret of scripts.registry.pushSecret and its rights on the repository" {
		t.Errorf("a plain 401: %q", got)
	}
	// a network error after the denial is not what came before it
	after := plain + "dial tcp 10.0.0.1:5000: i/o timeout\n"
	if strings.Contains(sbPushDeniedSentence(after), "expired") {
		t.Error("a network error after the 401 changed the sentence")
	}
}
