// Package bayescompile is the pure part of stan-compile (docs/specs/bayes-stan-compile.md): the cache key of a
// program's binary, the states of a program's compile and their transitions, the owner's rules on the queue (one
// compile per person, «Compiles at once», a program that passed a limit not compiled again under it, «Compile now»,
// three attempts for the cluster's failures), the cache's eviction order under its limit, and the compile Job's
// termination message. Nothing here reads the cluster, and the rules never read the database: the loop (lot C2) and
// the Compiler (lot C3, compiler.go) lock the rows and apply what these functions decide, writing bayes_binaries through
// rows.go (Scan, Insert, Write, Delete).
package bayescompile

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
)

// KeyFormat is the key's first line: a later change of what the key holds changes it, and every old key stops
// matching (their binaries age out of the cache).
const KeyFormat = "bayes-binary/1"

// DefaultArch is the architecture of an image that does not name one (the image spec claims arm64 only after its
// self-test).
const DefaultArch = "amd64"

var (
	hex64   = regexp.MustCompile(`^[0-9a-f]{64}$`)
	digest  = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	version = regexp.MustCompile(`^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$`)
	arches  = map[string]bool{"amd64": true, "arm64": true}
)

// CmdStanOf is the CmdStan version a group's recipe builds (its sources named cmdstan, one per machine, of one
// version): a key's cmdstan line; "" when the recipe builds none.
func CmdStanOf(r modelgroups.Recipe) string {
	for _, s := range r.Sources {
		if s.Name == "cmdstan" {
			return s.Version
		}
	}
	return ""
}

// ErrKey is a part of the key that is not of its form: never a key built from it.
var ErrKey = errors.New("bayes compile: not a cache key's part")

// Image is the group version's image a binary is compiled in and valid for (the image spec: «a binary is valid only
// with the image that compiled it»): CmdStan's version, the architecture (empty: amd64) and the image's digest.
type Image struct {
	CmdStan string // 2.40.0
	Arch    string // amd64, arm64; empty is amd64
	Digest  string // sha256:<64 hex digits>
}

// KeyLines are the seven lines a cache key is the sha256 of, each ending with a newline. stanc= is empty because
// stanc runs with make's own arguments and no flag; cxx=image because the C++ flags are the image's alone and its
// digest already names them.
func KeyLines(programSHA256 string, img Image) (string, error) {
	arch := img.Arch
	if arch == "" {
		arch = DefaultArch
	}
	switch {
	case !hex64.MatchString(programSHA256):
		return "", fmt.Errorf("%w: the program's sha256 %q", ErrKey, programSHA256)
	case !version.MatchString(img.CmdStan):
		return "", fmt.Errorf("%w: CmdStan's version %q", ErrKey, img.CmdStan)
	case !arches[arch]:
		return "", fmt.Errorf("%w: the architecture %q", ErrKey, img.Arch)
	case !digest.MatchString(img.Digest):
		return "", fmt.Errorf("%w: the image's digest %q", ErrKey, img.Digest)
	}
	return KeyFormat + "\n" +
		"program=" + programSHA256 + "\n" +
		"cmdstan=" + img.CmdStan + "\n" +
		"stanc=\n" +
		"cxx=image\n" +
		"arch=" + arch + "\n" +
		"image=" + img.Digest + "\n", nil
}

// CacheKey is the sha256 (64 hex digits) of KeyLines: the row of bayes_binaries a program has for an image. A change
// of any line (a new CmdStan, a rebuild, the other architecture) is a new key, so a binary is never run with an image
// other than its own.
func CacheKey(programSHA256 string, img Image) (string, error) {
	lines, err := KeyLines(programSHA256, img)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256([]byte(lines))
	return hex.EncodeToString(sum[:]), nil
}
