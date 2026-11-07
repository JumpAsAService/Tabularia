package config

import (
	"encoding/json"
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
	// DATA_API__POLL_SECONDS: how often the pods ask for their manifest (a revoked key, a
	// refresh, an edit reach them within it); the manifest carries it to them
	PollSeconds int
	// DATA_API__USAGE_RETENTION_DAYS: how long the usage per key and minute is kept
	UsageRetentionDays int

	// The pods, as the orchestrator's reconcile loop makes them (DataAPIPods)
	Pods DataAPIPods
}

// DataAPIPods is how the reconcile loop runs a data API's pods and opens them to the other
// systems; the Helm chart sets every value.
type DataAPIPods struct {
	// DATA_API__NAMESPACE: where the pods and their objects go (default: SCRIPTS__NAMESPACE)
	Namespace string
	// KUBERNETES__RELEASE: the release whose objects these are (their names and labels)
	Release string
	// DATA_API__IMAGE, DATA_API__IMAGE_PULL_POLICY, DATA_API__IMAGE_PULL_SECRETS (names,
	// comma-separated, of Secrets of the namespace): the pods' image, tabularia-data-api
	Image            string
	ImagePullPolicy  string
	ImagePullSecrets []string
	// DATA_API__GATEWAY_URL: the gateway's address inside the cluster, which the pods ask for
	// their manifest (default: NOTEBOOKS__GATEWAY_URL)
	GatewayURL string
	// DATA_API__FORWARDED_ALLOW_IPS: the proxies the pods believe the X-Forwarded-For of (the
	// ingress controller), for the keys' address limits
	ForwardedAllowIPs string
	// DATA_API__MAX_DISK: the most disk a pod is given for its data files (a Kubernetes
	// quantity); each gets what its parquets need, rounded up to a power of two
	MaxDisk string
	// DATA_API__INGRESS_CLASS, DATA_API__INGRESS_HOST (empty: any host),
	// DATA_API__INGRESS_TLS_SECRET (a Secret of the namespace; empty: none),
	// DATA_API__INGRESS_ANNOTATIONS (a JSON object): the Ingress of each data API, with
	// /data/{slug}/ only. The chart puts dataApi.publicHost, when set, as the host, with its
	// certificate
	IngressClass       string
	IngressHost        string
	IngressTLSSecret   string
	IngressAnnotations map[string]string
	// DATA_API__PUBLIC_BASE_URL: the address the other systems call the data APIs at, as the
	// pages and the documentation write it (https://api.example.com); empty: the interface's
	// own address, as the browser has it
	PublicBaseURL string
	// DATA_API__NETWORK_POLICY: each data API gets a NetworkPolicy: in, the peers of
	// DATA_API__NETWORK_POLICY_INGRESS_FROM (a JSON list of NetworkPolicyPeer: the ingress
	// controller) and nothing else; out, DNS and the rules of DATA_API__NETWORK_POLICY_EGRESS
	// (a JSON list of NetworkPolicyEgressRule: the gateway, the object storage) and nothing else
	NetworkPolicy bool
	IngressFrom   []any
	Egress        []any
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

// jsonList is a variable holding a JSON list of objects; nil when it is empty
func jsonList(r *reader, name string) []any {
	text := strings.TrimSpace(r.str(name, ""))
	if text == "" {
		return nil
	}
	var list []any
	if err := json.Unmarshal([]byte(text), &list); err != nil {
		r.fail(name, "is not a JSON list: "+err.Error())
		return nil
	}
	for _, it := range list {
		if _, ok := it.(map[string]any); !ok {
			r.fail(name, "is not a JSON list of objects")
			return nil
		}
	}
	return list
}

func loadDataAPI(r *reader) DataAPIConfig {
	c := DataAPIConfig{
		Enabled:            r.boolean("DATA_API__ENABLED", false),
		MaxAPIs:            r.integer("DATA_API__MAX_APIS", 20, 0, 10000),
		MaxReplicas:        r.integer("DATA_API__MAX_REPLICAS", 3, 1, 100),
		MaxTotalReplicas:   r.integer("DATA_API__MAX_TOTAL_REPLICAS", 10, 1, 1000),
		MaxCPU:             strings.TrimSpace(r.str("DATA_API__MAX_CPU", "1")),
		MaxMemory:          strings.TrimSpace(r.str("DATA_API__MAX_MEMORY", "1Gi")),
		KeyMaxDays:         r.integer("DATA_API__KEY_MAX_DAYS", 365, 1, 3650),
		KeyDefaultRate:     r.integer("DATA_API__KEY_DEFAULT_RATE", 600, 1, 1_000_000),
		PollSeconds:        r.integer("DATA_API__POLL_SECONDS", 20, 5, 600),
		UsageRetentionDays: r.integer("DATA_API__USAGE_RETENTION_DAYS", 30, 1, 3650),
	}
	p := &c.Pods
	p.Namespace = strings.TrimSpace(r.str("DATA_API__NAMESPACE", ""))
	if p.Namespace == "" {
		p.Namespace = strings.TrimSpace(r.str("SCRIPTS__NAMESPACE", ""))
	}
	p.Release = strings.TrimSpace(r.str("KUBERNETES__RELEASE", ""))
	if p.Release == "" {
		p.Release = "tabularia"
	}
	p.Image = strings.TrimSpace(r.str("DATA_API__IMAGE", ""))
	p.ImagePullPolicy = strings.TrimSpace(r.str("DATA_API__IMAGE_PULL_POLICY", "IfNotPresent"))
	switch p.ImagePullPolicy {
	case "Always", "IfNotPresent", "Never":
	default:
		r.fail("DATA_API__IMAGE_PULL_POLICY", fmt.Sprintf("is not Always, IfNotPresent or Never: %q", p.ImagePullPolicy))
		p.ImagePullPolicy = "IfNotPresent"
	}
	for _, name := range strings.Split(r.str("DATA_API__IMAGE_PULL_SECRETS", ""), ",") {
		if name = strings.TrimSpace(name); name != "" {
			p.ImagePullSecrets = append(p.ImagePullSecrets, name)
		}
	}
	p.GatewayURL = strings.TrimRight(strings.TrimSpace(r.str("DATA_API__GATEWAY_URL", "")), "/")
	if p.GatewayURL == "" {
		p.GatewayURL = strings.TrimRight(strings.TrimSpace(r.str("NOTEBOOKS__GATEWAY_URL", "")), "/")
	}
	p.ForwardedAllowIPs = strings.TrimSpace(r.str("DATA_API__FORWARDED_ALLOW_IPS", ""))
	p.MaxDisk = strings.TrimSpace(r.str("DATA_API__MAX_DISK", "20Gi"))
	if b, err := MemoryBytes(p.MaxDisk); err != nil || b < 1<<30 {
		r.fail("DATA_API__MAX_DISK", fmt.Sprintf("is not a quantity of at least 1Gi: %q", p.MaxDisk))
		p.MaxDisk = "20Gi"
	}
	p.IngressClass = strings.TrimSpace(r.str("DATA_API__INGRESS_CLASS", ""))
	p.IngressHost = strings.TrimSpace(r.str("DATA_API__INGRESS_HOST", ""))
	p.IngressTLSSecret = strings.TrimSpace(r.str("DATA_API__INGRESS_TLS_SECRET", ""))
	p.PublicBaseURL = strings.TrimRight(strings.TrimSpace(r.str("DATA_API__PUBLIC_BASE_URL", "")), "/")
	if p.PublicBaseURL != "" && !strings.HasPrefix(p.PublicBaseURL, "https://") && !strings.HasPrefix(p.PublicBaseURL, "http://") {
		r.fail("DATA_API__PUBLIC_BASE_URL", fmt.Sprintf("is not an http(s) address: %q", p.PublicBaseURL))
		p.PublicBaseURL = ""
	}
	if text := strings.TrimSpace(r.str("DATA_API__INGRESS_ANNOTATIONS", "")); text != "" {
		if err := json.Unmarshal([]byte(text), &p.IngressAnnotations); err != nil {
			r.fail("DATA_API__INGRESS_ANNOTATIONS", "is not a JSON object of texts: "+err.Error())
			p.IngressAnnotations = nil
		}
	}
	p.NetworkPolicy = r.boolean("DATA_API__NETWORK_POLICY", true)
	p.IngressFrom = jsonList(r, "DATA_API__NETWORK_POLICY_INGRESS_FROM")
	p.Egress = jsonList(r, "DATA_API__NETWORK_POLICY_EGRESS")
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
