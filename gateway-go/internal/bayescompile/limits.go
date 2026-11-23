package bayescompile

import (
	"fmt"
	"regexp"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
)

// DefaultSlots is «Compiles at once» when the administrator set nothing: 2 (2 CPU and 6 GiB at the default limits).
const DefaultSlots = 2

// The floors of the card (lot C4): a memory below 2 GiB or a deadline below 60 s is refused, with the measured peaks
// and times quoted (1.3–1.9 GiB, 11–45 s; the 1,497-node budget 130.5 s).
const (
	MinMemoryBytes     = 2 << 30
	MinDeadlineSeconds = 60
)

var (
	quantity = regexp.MustCompile(`^([1-9][0-9]{0,6})(Mi|Gi)$`) // the recipe's form (modelgroups)
	cores    = regexp.MustCompile(`^[1-9][0-9]{0,2}$`)
)

// Settings is the row of bayes_settings (id 1) as the rules read it: nil is NULL, the recipe's default.
type Settings struct {
	CompileCPU     *string
	CompileMemory  *string
	CompileSeconds *int64
	CompileTmp     *string
	CompileSlots   *int
	CacheMaxBytes  *int64
}

// Limits are the compile limits in force: the administrator's values over the recipe's defaults. A compile Job runs
// under them, MayRetry compares a timeout's recorded limits with them, the queue fills Slots, the cache keeps to
// CacheMaxBytes.
type Limits struct {
	CPU             string // whole cores, the Job's request and limit
	Memory          string // the Job's request and limit, as written (3Gi)
	MemoryBytes     int64
	DeadlineSeconds int64  // activeDeadlineSeconds
	Tmp             string // the /tmp emptyDir's sizeLimit, as written (512Mi)
	TmpBytes        int64
	Slots           int // «Compiles at once»
	CacheMaxBytes   int64
}

// Resolve is the limits in force: each setting the administrator set, else the recipe's default (recipe.json's compile
// and cache), else DefaultSlots for the slots. A value not of its form is an error, never a guess.
func Resolve(compile *modelgroups.CompileLimits, cache *modelgroups.CacheLimits, s Settings) (Limits, error) {
	if compile == nil || cache == nil {
		return Limits{}, fmt.Errorf("bayes compile: the recipe has no compile or cache defaults")
	}
	l := Limits{CPU: compile.CPU, Memory: compile.Memory, DeadlineSeconds: compile.Seconds, Tmp: compile.Tmp,
		Slots: DefaultSlots, CacheMaxBytes: cache.MaxBytes}
	if s.CompileCPU != nil {
		l.CPU = *s.CompileCPU
	}
	if s.CompileMemory != nil {
		l.Memory = *s.CompileMemory
	}
	if s.CompileSeconds != nil {
		l.DeadlineSeconds = *s.CompileSeconds
	}
	if s.CompileTmp != nil {
		l.Tmp = *s.CompileTmp
	}
	if s.CompileSlots != nil {
		l.Slots = *s.CompileSlots
	}
	if s.CacheMaxBytes != nil {
		l.CacheMaxBytes = *s.CacheMaxBytes
	}
	var err error
	if !cores.MatchString(l.CPU) {
		return Limits{}, fmt.Errorf("bayes compile: the compile CPU %q", l.CPU)
	}
	if l.MemoryBytes, err = QuantityBytes(l.Memory); err != nil {
		return Limits{}, err
	}
	if l.TmpBytes, err = QuantityBytes(l.Tmp); err != nil {
		return Limits{}, err
	}
	switch {
	case l.DeadlineSeconds <= 0:
		return Limits{}, fmt.Errorf("bayes compile: the longest compile %d s", l.DeadlineSeconds)
	case l.Slots <= 0:
		return Limits{}, fmt.Errorf("bayes compile: %d compiles at once", l.Slots)
	case l.CacheMaxBytes <= 0:
		return Limits{}, fmt.Errorf("bayes compile: the cache's limit %d bytes", l.CacheMaxBytes)
	}
	return l, nil
}

// QuantityBytes is a quantity of the recipe's form (a whole number of Mi or Gi) in bytes.
func QuantityBytes(q string) (int64, error) {
	m := quantity.FindStringSubmatch(q)
	if m == nil {
		return 0, fmt.Errorf("bayes compile: the quantity %q is not a whole number of Mi or Gi", q)
	}
	n, _ := strconv.ParseInt(m[1], 10, 64)
	if m[2] == "Gi" {
		return n << 30, nil
	}
	return n << 20, nil
}

// Bytes is a size for a sentence: whole GiB or MiB when exact, else MiB with one decimal (2 GiB, 512 MiB, 2.9 MiB).
func Bytes(n int64) string {
	switch {
	case n >= 1<<30 && n%(1<<30) == 0:
		return strconv.FormatInt(n>>30, 10) + " GiB"
	case n >= 1<<20 && n%(1<<20) == 0:
		return strconv.FormatInt(n>>20, 10) + " MiB"
	case n >= 1<<30:
		return strconv.FormatFloat(float64(n)/(1<<30), 'f', 1, 64) + " GiB"
	case n >= 1<<20:
		return strconv.FormatFloat(float64(n)/(1<<20), 'f', 1, 64) + " MiB"
	}
	return strconv.FormatInt(n, 10) + " bytes"
}
