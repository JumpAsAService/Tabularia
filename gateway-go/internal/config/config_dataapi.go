package config

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// DataAPIConfig is what the data APIs read of the environment (docs/specs/data-apis.md): a
// feature of the Go gateway only, with no twin in the Python settings. The administrator's
// caps; the panel's «Data APIs» category may change them later.
type DataAPIConfig struct {
	// DATA_API__ENABLED: the feature, on Kubernetes only (the scripting's settings say where
	// that is); off by default
	Enabled bool
	// DATA_API__MAX_APIS: the most data APIs in the installation
	MaxAPIs int
	// DATA_API__MAX_REPLICAS, DATA_API__MAX_TOTAL_REPLICAS: the most pods of one data API, and
	// of all the active ones together
	MaxReplicas      int
	MaxTotalReplicas int
	// DATA_API__MAX_CPU, DATA_API__MAX_MEMORY: the largest pod, as Kubernetes quantities
	MaxCPU    string
	MaxMemory string
	// DATA_API__KEY_MAX_DAYS: a key's longest life; DATA_API__KEY_DEFAULT_RATE: the requests
	// per minute of a key made without a rate
	KeyMaxDays     int
	KeyDefaultRate int
}

var (
	cpuQuantity    = regexp.MustCompile(`^([0-9]+(?:\.[0-9]+)?)(m?)$`)
	memoryQuantity = regexp.MustCompile(`^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?$`)
)

// Millicores reads a CPU quantity («500m», «2», «1.5») as thousandths of a core.
func Millicores(q string) (int64, error) {
	m := cpuQuantity.FindStringSubmatch(strings.TrimSpace(q))
	if m == nil {
		return 0, fmt.Errorf("%q is not a CPU quantity (such as 500m or 2)", q)
	}
	f, _ := strconv.ParseFloat(m[1], 64)
	if m[2] == "" {
		f *= 1000
	}
	return int64(f + 0.5), nil
}

var memoryUnits = map[string]float64{"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
	"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40}

// MemoryBytes reads a memory quantity («512Mi», «2Gi», «1G») as bytes.
func MemoryBytes(q string) (int64, error) {
	m := memoryQuantity.FindStringSubmatch(strings.TrimSpace(q))
	if m == nil {
		return 0, fmt.Errorf("%q is not a memory quantity (such as 512Mi or 2Gi)", q)
	}
	f, _ := strconv.ParseFloat(m[1], 64)
	return int64(f * memoryUnits[m[2]]), nil
}

func loadDataAPI(r *reader) DataAPIConfig {
	c := DataAPIConfig{
		Enabled:          r.boolean("DATA_API__ENABLED", false),
		MaxAPIs:          r.integer("DATA_API__MAX_APIS", 20, 0, 10000),
		MaxReplicas:      r.integer("DATA_API__MAX_REPLICAS", 3, 1, 100),
		MaxTotalReplicas: r.integer("DATA_API__MAX_TOTAL_REPLICAS", 10, 1, 1000),
		MaxCPU:           strings.TrimSpace(r.str("DATA_API__MAX_CPU", "1")),
		MaxMemory:        strings.TrimSpace(r.str("DATA_API__MAX_MEMORY", "1Gi")),
		KeyMaxDays:       r.integer("DATA_API__KEY_MAX_DAYS", 365, 1, 3650),
		KeyDefaultRate:   r.integer("DATA_API__KEY_DEFAULT_RATE", 600, 1, 1_000_000),
	}
	if _, err := Millicores(c.MaxCPU); err != nil {
		r.fail("DATA_API__MAX_CPU", err.Error())
		c.MaxCPU = "1"
	}
	if _, err := MemoryBytes(c.MaxMemory); err != nil {
		r.fail("DATA_API__MAX_MEMORY", err.Error())
		c.MaxMemory = "1Gi"
	}
	return c
}
