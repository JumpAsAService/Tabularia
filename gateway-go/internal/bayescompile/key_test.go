package bayescompile

import (
	"errors"
	"strings"
	"testing"
)

var (
	progA = strings.Repeat("a", 64)
	progB = strings.Repeat("c", 64)
	imgB  = Image{CmdStan: "2.40.0", Arch: "amd64", Digest: "sha256:" + strings.Repeat("b", 64)}
)

// The seven lines and their sha256 for a fixed input (golden: computed outside Go with printf | sha256sum).
func TestCacheKeyGolden(t *testing.T) {
	lines, err := KeyLines(progA, imgB)
	if err != nil {
		t.Fatal(err)
	}
	want := "bayes-binary/1\nprogram=" + progA + "\ncmdstan=2.40.0\nstanc=\ncxx=image\narch=amd64\nimage=sha256:" +
		strings.Repeat("b", 64) + "\n"
	if lines != want {
		t.Fatalf("lines:\n%s\nwant:\n%s", lines, want)
	}
	key, err := CacheKey(progA, imgB)
	if err != nil || key != "0565b704f2ea1a596d03074f5fadc3fe16bba793d744fa52f07a05a726531bee" {
		t.Fatalf("key %s, %v", key, err)
	}
	// an image that names no architecture is amd64's
	noArch := imgB
	noArch.Arch = ""
	if k, _ := CacheKey(progA, noArch); k != key {
		t.Fatalf("empty arch: %s", k)
	}
}

// A change of any line is another key.
func TestCacheKeyEveryLineCounts(t *testing.T) {
	base, _ := CacheKey(progA, imgB)
	for name, k := range map[string]func() (string, error){
		"program": func() (string, error) { return CacheKey(progB, imgB) },
		"cmdstan": func() (string, error) { i := imgB; i.CmdStan = "2.41.0"; return CacheKey(progA, i) },
		"arch":    func() (string, error) { i := imgB; i.Arch = "arm64"; return CacheKey(progA, i) },
		"image": func() (string, error) {
			i := imgB
			i.Digest = "sha256:" + strings.Repeat("d", 64)
			return CacheKey(progA, i)
		},
	} {
		got, err := k()
		if err != nil || got == base || len(got) != 64 {
			t.Errorf("%s: %s, %v", name, got, err)
		}
	}
}

// A part not of its form is refused, never written into a line (a newline would forge another key).
func TestCacheKeyRefusesForeignParts(t *testing.T) {
	for name, c := range map[string]struct {
		prog string
		img  Image
	}{
		"short program":     {progA[:63], imgB},
		"upper hex":         {strings.ToUpper(progA), imgB},
		"newline":           {progA[:63] + "\n", imgB},
		"cmdstan":           {progA, Image{CmdStan: "2.40\nx", Digest: imgB.Digest}},
		"empty cmdstan":     {progA, Image{Digest: imgB.Digest}},
		"arch":              {progA, Image{CmdStan: "2.40.0", Arch: "x86_64", Digest: imgB.Digest}},
		"digest no prefix":  {progA, Image{CmdStan: "2.40.0", Digest: strings.Repeat("b", 64)}},
		"digest a tag":      {progA, Image{CmdStan: "2.40.0", Digest: "tabularia/bayes:1"}},
		"digest with extra": {progA, Image{CmdStan: "2.40.0", Digest: imgB.Digest + "\nimage=x"}},
	} {
		if _, err := CacheKey(c.prog, c.img); !errors.Is(err, ErrKey) {
			t.Errorf("%s: %v", name, err)
		}
	}
}
