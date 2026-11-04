package settings

import (
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// The whole of overrides.ITEMS and overrides.check, for the panel's routes
// (routes/gateway_settings.py): what Administration → Settings lists, and the check of what it
// saves. The values the Go gateway applies are still read by settings.go and
// settings_admin.go, one reader per key; this file only says what the panel shows and accepts.

// MinMemory is overrides.MIN_MEMORY.
const MinMemory = 64 * 1024 * 1024

const day = 86_400

// Item is overrides.Item.
type Item struct {
	Key, Group, Kind, Unit string
	// Min and Max as ITEMS writes them: int64, float64 or nil (an int stays an int in an error)
	Min, Max  any
	MaxLength int
	Required  bool
	Rollout   []string
	When      string // sso | kubernetes | scripts | containers
}

// EnvVar is Item.env_var: «SECTION__FIELD».
func (it *Item) EnvVar() string {
	section, field, _ := strings.Cut(it.Key, ".")
	return strings.ToUpper(section) + "__" + strings.ToUpper(field)
}

func i(n int64) any   { return n }
func f(x float64) any { return x }

var gw = []string{"gateway", "orchestrator"}

func pitem(key, group, kind, unit string, min, max any) Item {
	return Item{Key: key, Group: group, Kind: kind, Unit: unit, Min: min, Max: max, MaxLength: 200}
}

func when(it Item, w string) Item        { it.When = w; return it }
func maxLen(it Item, n int) Item         { it.MaxLength = n; return it }
func required(it Item) Item              { it.Required = true; return it }
func rollout(it Item, c []string) Item   { it.Rollout = c; return it }
func plain(key, group, kind string) Item { return pitem(key, group, kind, "", nil, nil) }

// Items is overrides.ITEMS, in its order.
var Items = []Item{
	pitem("jwt.access_ttl_minutes", "signin", "int", "minutes", i(15), i(7*24*60)),
	pitem("auth.session_idle_minutes", "signin", "int", "minutes", i(0), i(24*60)),
	plain("auth.personal_tokens", "signin", "bool"),
	pitem("auth.api_token_max_days", "signin", "int", "days", i(1), i(3650)),
	when(required(maxLen(plain("oidc.groups_claim", "sso", "str"), 500)), "sso"),
	when(maxLen(plain("oidc.group_allowlist", "sso", "str"), 4000), "sso"),
	when(plain("oidc.auto_create_groups", "sso", "bool"), "sso"),
	when(plain("oidc.authoritative", "sso", "bool"), "sso"),
	when(maxLen(plain("oidc.allowed_email_domains", "sso", "str"), 4000), "sso"),
	when(plain("oidc.require_allowlisted_group", "sso", "bool"), "sso"),
	when(maxLen(plain("oidc.button_label", "sso", "str"), 60), "sso"),
	when(pitem("oidc.discovery_ttl_seconds", "sso", "int", "seconds", i(60), i(day)), "sso"),
	when(pitem("oidc.timeout_seconds", "sso", "float", "seconds", i(1), i(60)), "sso"),
	when(pitem("oidc.login_tx_ttl_seconds", "sso", "int", "seconds", i(120), i(3600)), "sso"),
	plain("app.timezone", "orchestration", "timezone"),
	pitem("engine.run_stale_timeout_seconds", "orchestration", "int", "seconds", i(900), i(7*day)),
	pitem("orchestrator.refresh_wait_seconds", "orchestration", "int", "seconds", i(60), i(day)),
	pitem("orchestrator.poll_interval_seconds", "orchestration", "float", "seconds", f(0.5), i(60)),
	pitem("orchestrator.queue_poll_seconds", "orchestration", "float", "seconds", f(0.2), i(30)),
	pitem("orchestrator.max_concurrent", "orchestration", "int", "runs", i(1), i(200)),
	pitem("orchestrator.queue_timeout_seconds", "orchestration", "float", "seconds", i(60), i(day)),
	pitem("orchestrator.heartbeat_seconds", "orchestration", "float", "seconds", i(2), i(120)),
	pitem("orchestrator.dead_after_seconds", "orchestration", "float", "seconds", i(15), i(3600)),
	pitem("scheduling.worker_capacity", "orchestration", "int", "runs", i(1), i(256)),
	pitem("orchestrator.log_retention_days", "history", "int", "days", i(0), i(3650)),
	pitem("orchestrator.contract_results_kept", "history", "int", "reports", i(0), i(100_000)),
	pitem("orchestrator.versions_kept", "history", "int", "versions", i(0), i(10_000)),
	pitem("orchestrator.ai_chat_retention_days", "history", "int", "days", i(0), i(3650)),
	pitem("webhooks.retention_days", "history", "int", "days", i(1), i(3650)),
	pitem("webhooks.timeout_seconds", "webhooks", "float", "seconds", i(1), i(60)),
	plain("ai.metadata_only", "ai", "bool"),
	plain("ai.default_model", "ai", "str"),
	pitem("ai.max_result_rows", "ai", "int", "rows", i(1), i(2000)),
	pitem("ai.max_requests", "ai", "int", "requests", i(1), i(50)),
	pitem("ai.max_flow_requests", "ai", "int", "requests", i(1), i(80)),
	pitem("ai.max_history_messages", "ai", "int", "messages", i(0), i(400)),
	pitem("ai.max_cost_per_turn_usd", "ai", "float", "usd", i(0), i(100)),
	pitem("ai.max_cost_per_day_usd", "ai", "float", "usd", i(0), i(10_000)),
	pitem("ai.max_cost_per_day_total_usd", "ai", "float", "usd", i(0), i(100_000)),
	pitem("ai.max_input_tokens_per_turn", "ai", "int", "tokens", i(1000), i(2_000_000)),
	pitem("ai.max_chats_listed", "ai", "int", "chats", i(1), i(500)),
	pitem("ai.dbt_export_timeout_seconds", "ai", "float", "seconds", i(10), i(3600)),
	plain("ai.comment_model", "ai", "str"),
	pitem("ai.comment_keep_seconds", "ai", "int", "seconds", i(60), i(30*day)),
	pitem("ai.comment_min_seconds", "ai", "int", "seconds", i(60), i(day)),
	pitem("ai.comment_wait_seconds", "ai", "int", "seconds", i(1), i(240)),
	plain("mcp.enabled", "mcp", "bool"),
	plain("mcp.confirm_in_tabularia_only", "mcp", "bool"),
	plain("mcp.metadata_only", "mcp", "bool"),
	pitem("engine.dbt_export_timeout_seconds", "dbt", "float", "seconds", i(10), i(3600)),
	pitem("preview.default_sample_rows", "previews", "int", "rows", i(0), i(10_000_000)),
	pitem("upload.max_bytes", "previews", "int", "bytes", i(1<<20), i(1<<40)),
	pitem("backup.timeout_seconds", "backups", "int", "seconds", i(60), i(day)),
	pitem("monitoring.timeout_seconds", "monitoring", "float", "seconds", f(0.5), i(30)),
	pitem("monitoring.cache_seconds", "monitoring", "float", "seconds", i(0), i(300)),
	pitem("openlineage.timeout_seconds", "openlineage", "float", "seconds", i(1), i(120)),
	rollout(pitem("app.gateway_threads", "processes", "int", "threads", i(4), i(200)), gw),
	rollout(pitem("db.pool_size", "processes", "int", "connections", i(1), i(200)), gw),
	rollout(pitem("db.max_overflow", "processes", "int", "connections", i(0), i(200)), gw),
	when(pitem("kubernetes.timeout_seconds", "kubernetes", "float", "seconds", i(1), i(120)), "kubernetes"),
	when(pitem("scripts.poll_seconds", "scripts", "float", "seconds", i(1), i(60)), "scripts"),
	when(pitem("scripts.job_ttl_seconds", "scripts", "int", "seconds", i(60), i(7*day)), "scripts"),
	when(pitem("scripts.log_max_bytes", "scripts", "int", "bytes", i(4096), i(16*1024*1024)), "scripts"),
	when(plain("scripts.lint", "scripts", "bool"), "scripts"),
	when(pitem("scripts.build_timeout_seconds", "builds", "int", "seconds", i(60), i(6*3600)), "scripts"),
	when(plain("scripts.build_cpu", "builds", "cpu"), "scripts"),
	when(plain("scripts.build_memory", "builds", "memory"), "scripts"),
	when(pitem("scripts.check_timeout_seconds", "builds", "int", "seconds", i(30), i(3600)), "scripts"),
	when(pitem("notebooks.max_visitors_per_app", "notebooks", "int", "people", i(1), i(1000)), "scripts"),
	when(pitem("notebooks.max_visitors_ceiling", "notebooks", "int", "people", i(1), i(1000)), "scripts"),
	when(pitem("containers.pull_check_seconds", "containers", "int", "seconds", i(30), i(3600)), "containers"),
	when(pitem("containers.poll_seconds", "containers", "float", "seconds", i(1), i(60)), "containers"),
	when(pitem("containers.job_ttl_seconds", "containers", "int", "seconds", i(60), i(7*day)), "containers"),
	when(plain("containers.max_cpu", "containers", "cpu"), "containers"),
	when(plain("containers.max_memory", "containers", "memory"), "containers"),
	when(pitem("containers.max_timeout_seconds", "containers", "int", "seconds", i(60), i(30*day)), "containers"),
	when(pitem("containers.log_max_bytes", "containers", "int", "bytes", i(4096), i(16*1024*1024)), "containers"),
}

// PerKey is overrides.PER_KEY.
var PerKey = func() map[string]*Item {
	m := map[string]*Item{}
	for k := range Items {
		m[Items[k].Key] = &Items[k]
	}
	return m
}()

// ── checking one value, and the set ─────────────────────────────────────────

func failure(key, code string, more ...pyjson.KV) pyjson.Object {
	return append(pyjson.Object{{Key: "key", Value: key}, {Key: "code", Value: code}}, more...)
}

// rat is a Python number (int or finite float) exactly, for the comparisons.
func rat(v any) *big.Rat {
	switch x := v.(type) {
	case int64:
		return new(big.Rat).SetInt64(x)
	case *big.Int:
		return new(big.Rat).SetInt(x)
	case float64:
		return new(big.Rat).SetFloat64(x)
	}
	return nil
}

// below is `a < b` of two Python numbers.
func below(a, b any) bool { return rat(a).Cmp(rat(b)) < 0 }

// Python's \d: any decimal digit (Nd), which float() reads too.
var (
	cpuQuantity    = regexp.MustCompile(`^(\p{Nd}+(?:\.\p{Nd}+)?)(n|u|m)?$`)
	memoryQuantity = regexp.MustCompile(`^(\p{Nd}+(?:\.\p{Nd}+)?)(Ki|Mi|Gi|Ti|k|M|G|T|e\p{Nd}+)?$`)
	cpuForm        = regexp.MustCompile(`^\p{Nd}+(\.\p{Nd}+)?m?$`)
	memoryForm     = regexp.MustCompile(`^\p{Nd}+(\.\p{Nd}+)?(Ki|Mi|Gi|Ti|K|M|G|T)?$`)
)

// asciiDigits writes the decimal digits of any script as ASCII, as float() reads them.
func asciiDigits(s string) string {
	return strings.Map(func(r rune) rune {
		if unicode.Is(unicode.Nd, r) {
			return '0' + rune(digitValue(r))
		}
		return r
	}, s)
}

func digitValue(r rune) int {
	// the decimal digits come in runs of ten, 0 first
	for d := 0; d < 10; d++ {
		if !unicode.Is(unicode.Nd, r-rune(d)) {
			return d - 1
		}
	}
	return 9
}

func pyFloat(s string) float64 {
	x, _ := strconv.ParseFloat(asciiDigits(s), 64)
	return x
}

// cpuMillis is cluster_view.cpu_millis (ok false: None).
func cpuMillis(q string) (float64, bool) {
	m := cpuQuantity.FindStringSubmatch(pyd.Strip(q))
	if m == nil {
		return 0, false
	}
	v := pyFloat(m[1])
	switch m[2] {
	case "n":
		return v / 1e6, true
	case "u":
		return v / 1e3, true
	case "m":
		return v, true
	}
	return v * 1000, true
}

// memoryBytes is cluster_view.memory_bytes (ok false: None, or a power int() cannot take).
func memoryBytes(q string) (float64, bool) {
	m := memoryQuantity.FindStringSubmatch(pyd.Strip(q))
	if m == nil {
		return 0, false
	}
	v, unit := pyFloat(m[1]), m[2]
	factor := map[string]float64{"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
	if strings.HasPrefix(unit, "e") {
		exp, _ := strconv.Atoi(asciiDigits(unit[1:]))
		x := v * math.Pow(10, float64(exp))
		if math.IsInf(x, 0) || math.IsNaN(x) {
			return 0, false // int() of inf: an OverflowError the check does not catch — treated as no size
		}
		return math.Trunc(x), true
	}
	if fa, ok := factor[unit]; ok {
		return math.Trunc(v * fa), true
	}
	return math.Trunc(v), true
}

// sectionCheck is the setting's own validation (`type(section).model_validate`) where it says
// more than the panel's range: the validators of core/config.py. ("", true) passes.
func sectionCheck(key, value string) (string, bool) {
	switch key {
	case "app.timezone":
		if _, err := config.LoadZone(value); err != nil {
			return "Value error, Invalid APP__TIMEZONE: '" + value + "' — use an IANA name (e.g. 'Europe/Rome', 'UTC')", false
		}
	case "scripts.build_cpu":
		if !cpuForm.MatchString(value) || pyFloat(strings.TrimRight(value, "m")) <= 0 {
			return "Value error, SCRIPTS__BUILD_CPU is a Kubernetes CPU quantity: 2, 1.5 or 1500m", false
		}
	case "scripts.build_memory":
		if !memoryForm.MatchString(value) {
			return "Value error, SCRIPTS__BUILD_MEMORY is a Kubernetes memory quantity: 4Gi, 6144Mi", false
		}
	case "containers.max_cpu":
		if !cpuForm.MatchString(value) {
			return "Value error, a Kubernetes CPU quantity: 4, 1.5, 500m", false
		}
	case "containers.max_memory":
		if !memoryForm.MatchString(value) {
			return "Value error, a Kubernetes memory quantity: 16Gi, 512Mi", false
		}
	}
	return "", true
}

// cut is str(msg)[:n].
func cut(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// CheckOne is overrides.check_one: (the value as the setting holds it, nil) or (nil, an error
// with a code the panel translates). Values are pyd's (json.loads').
func CheckOne(it *Item, value any) (any, pyjson.Object) {
	switch it.Kind {
	case "bool":
		if _, ok := value.(bool); !ok {
			return nil, failure(it.Key, "boolean")
		}
	case "int":
		switch value.(type) {
		case int64, *big.Int:
		default:
			return nil, failure(it.Key, "integer")
		}
	case "float":
		switch x := value.(type) {
		case int64:
			value = float64(x)
		case *big.Int:
			fl, _ := new(big.Float).SetInt(x).Float64()
			if math.IsInf(fl, 0) { // float() of an integer too large: OverflowError
				return nil, failure(it.Key, "max", pyjson.KV{Key: "limit", Value: it.Max})
			}
			value = fl
		case float64:
			if math.IsInf(x, 0) || math.IsNaN(x) {
				return nil, failure(it.Key, "number")
			}
		default:
			return nil, failure(it.Key, "number")
		}
	default: // str, timezone, cpu, memory
		s, ok := value.(string)
		if !ok {
			return nil, failure(it.Key, "text")
		}
		s = pyd.Strip(s)
		value = s
		if pyd.Len(s) > it.MaxLength {
			return nil, failure(it.Key, "too_long", pyjson.KV{Key: "limit", Value: int64(it.MaxLength)})
		}
		if (it.Kind == "timezone" || it.Kind == "cpu" || it.Kind == "memory" || it.Required) && s == "" {
			return nil, failure(it.Key, "invalid")
		}
		if it.Kind == "cpu" || it.Kind == "memory" {
			var size float64
			var ok bool
			if it.Kind == "cpu" {
				size, ok = cpuMillis(s)
			} else {
				size, ok = memoryBytes(s)
			}
			if ok && ((it.Kind == "cpu" && size <= 0) || (it.Kind == "memory" && size < MinMemory)) {
				msg := "more than 0"
				if it.Kind == "memory" {
					msg = "at least 64Mi"
				}
				return nil, failure(it.Key, "too_small", pyjson.KV{Key: "message", Value: msg})
			}
		}
		if msg, ok := sectionCheck(it.Key, s); !ok {
			return nil, failure(it.Key, "invalid", pyjson.KV{Key: "message", Value: cut(msg, 300)})
		}
		return s, nil
	}
	if it.Min != nil && rat(value) != nil && below(value, it.Min) {
		return nil, failure(it.Key, "min", pyjson.KV{Key: "limit", Value: it.Min})
	}
	if it.Max != nil && rat(value) != nil && below(it.Max, value) {
		return nil, failure(it.Key, "max", pyjson.KV{Key: "limit", Value: it.Max})
	}
	return value, nil
}

// Effective is overrides._effective: the value given, else the environment's.
func Effective(env map[string]any, values pyjson.Object, key string) any {
	if v, ok := values.Get(key); ok {
		return v
	}
	return env[key]
}

func num(v any) float64 {
	switch x := v.(type) {
	case int64:
		return float64(x)
	case float64:
		return x
	case *big.Int:
		fl, _ := new(big.Float).SetInt(x).Float64()
		return fl
	}
	return 0
}

func truthy(v any) bool { return pyd.Truthy(v) }

func without(o pyjson.Object, key string) pyjson.Object {
	out := pyjson.Object{}
	for _, kv := range o {
		if kv.Key != key {
			out = append(out, kv)
		}
	}
	return out
}

// Check is overrides.check: (good values, errors), `values` in the order they are checked,
// `env` the environment's values (config.PanelEnv); when a constraint between two settings
// breaks, the one just changed (`changed`) is refused, else the panel's.
func Check(values pyjson.Object, env map[string]any, changed map[string]bool) (pyjson.Object, []pyjson.Object) {
	good := pyjson.Object{}
	errs := []pyjson.Object{}
	for _, kv := range values {
		it := PerKey[kv.Key]
		if it == nil {
			errs = append(errs, failure(kv.Key, "unknown"))
			continue
		}
		v, err := CheckOne(it, kv.Value)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		good = append(good, pyjson.KV{Key: kv.Key, Value: v})
	}
	blame := func(keys ...string) string {
		var mine []string
		for _, k := range keys {
			if _, ok := good.Get(k); ok {
				mine = append(mine, k)
			}
		}
		if len(mine) == 0 {
			return ""
		}
		for _, k := range mine {
			if changed[k] {
				return k
			}
		}
		return mine[0]
	}
	e := func(k string) any { return Effective(env, good, k) }
	for moved := true; moved; {
		moved = false
		if !(num(e("orchestrator.heartbeat_seconds"))*3 <= num(e("orchestrator.dead_after_seconds"))) {
			if k := blame("orchestrator.heartbeat_seconds", "orchestrator.dead_after_seconds"); k != "" {
				good = without(good, k)
				errs = append(errs, failure(k, "heartbeat_dead_after"))
				moved = true
				continue
			}
		}
		allow, _ := e("oidc.group_allowlist").(string)
		super, _ := env["oidc.superuser_group"].(string)
		if truthy(e("oidc.require_allowlisted_group")) && pyd.Strip(allow) == "" && pyd.Strip(super) == "" {
			if k := blame("oidc.require_allowlisted_group", "oidc.group_allowlist"); k != "" {
				good = without(good, k)
				errs = append(errs, failure(k, "allowlist_needed"))
				moved = true
				continue
			}
		}
		pool := e("db.pool_size")
		if !truthy(pool) {
			pool = e("app.gateway_threads")
		}
		limit := new(big.Int).Add(bigOf(pool), bigOf(e("db.max_overflow")))
		if bigOf(e("orchestrator.max_concurrent")).Cmp(limit) > 0 {
			if k := blame("orchestrator.max_concurrent", "db.pool_size", "app.gateway_threads", "db.max_overflow"); k != "" {
				good = without(good, k)
				errs = append(errs, failure(k, "concurrent_pool", pyjson.KV{Key: "limit", Value: intValue(limit)}))
				moved = true
				continue
			}
		}
		perApp, ceiling := e("notebooks.max_visitors_per_app"), e("notebooks.max_visitors_ceiling")
		if bigOf(perApp).Cmp(bigOf(ceiling)) > 0 {
			if k := blame("notebooks.max_visitors_per_app", "notebooks.max_visitors_ceiling"); k != "" {
				good = without(good, k)
				other, code, lim := "notebooks.max_visitors_per_app", "ceiling_below_visitors", perApp
				if strings.HasSuffix(k, "per_app") {
					other, code, lim = "notebooks.max_visitors_ceiling", "visitors_above_ceiling", ceiling
				}
				errs = append(errs, pyjson.Object{{Key: "key", Value: k}, {Key: "other", Value: other}, {Key: "code", Value: code},
					{Key: "limit", Value: lim}})
				moved = true
				continue
			}
		}
	}
	return good, errs
}

func bigOf(v any) *big.Int {
	switch x := v.(type) {
	case int64:
		return big.NewInt(x)
	case *big.Int:
		return x
	}
	return new(big.Int)
}

func intValue(b *big.Int) any {
	if b.IsInt64() {
		return b.Int64()
	}
	return pyjson.Int(b.String())
}

// Available is routes/gateway_settings.py available: the groups of a feature this
// installation does not have are not shown.
func Available(it *Item, cfg *config.Config, tokenMounted bool) bool {
	switch it.When {
	case "sso":
		return cfg.SSOEnabled()
	case "kubernetes":
		return tokenMounted
	case "scripts":
		return cfg.Flows.ScriptsEnabled
	case "containers":
		return cfg.Flows.ContainersEnabled
	}
	return true
}
