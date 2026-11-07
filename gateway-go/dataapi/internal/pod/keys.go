package pod

import (
	"math"
	"net/http"
	"net/netip"
	"strings"
	"sync"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// Keys are a manifest's keys, by hash, as the pod checks them: no call to the gateway per
// request.
type Keys struct {
	byHash map[string]*KeyRule
}

// KeyRule is a key and what it may do.
type KeyRule struct {
	dataapiwire.Key
	nets []netip.Prefix
}

// NewKeys reads a manifest's keys. An address range that does not read refuses every address:
// a key is never let in from wider than it says.
func NewKeys(list []dataapiwire.Key) *Keys {
	k := &Keys{byHash: map[string]*KeyRule{}}
	for _, key := range list {
		rule := &KeyRule{Key: key}
		for _, c := range key.CIDRs {
			p, err := netip.ParsePrefix(c)
			if err != nil {
				p = netip.Prefix{} // matches nothing
			}
			rule.nets = append(rule.nets, p)
		}
		k.byHash[key.Hash] = rule
	}
	return k
}

// KeyOf is the key a request sends: `Authorization: Bearer …` or `X-API-Key`; "" with none.
func KeyOf(r *http.Request) string {
	if h := r.Header.Get("Authorization"); h != "" {
		scheme, rest, _ := strings.Cut(h, " ")
		if strings.EqualFold(scheme, "bearer") {
			return strings.TrimSpace(rest)
		}
		return ""
	}
	return strings.TrimSpace(r.Header.Get("X-API-Key"))
}

// Check finds the key and says why it may not come in, nil when it may.
func (k *Keys) Check(key string, now time.Time) (*KeyRule, *Problem) {
	if key == "" {
		return nil, &Problem{Status: 401, Code: "key_missing",
			Detail: "A key is needed: send it as Authorization: Bearer tdk_… or in X-API-Key"}
	}
	if !dataapiwire.WellFormed(key, dataapiwire.KeyPrefix) {
		return nil, &Problem{Status: 401, Code: "key_invalid", Detail: "Not a key of this data API"}
	}
	rule := k.byHash[dataapiwire.Hash(key)]
	if rule == nil {
		return nil, &Problem{Status: 401, Code: "key_invalid", Detail: "Not a key of this data API"}
	}
	switch rule.State(now) {
	case dataapiwire.KeyRevoked:
		return rule, &Problem{Status: 401, Code: "key_revoked", Detail: "This key was revoked"}
	case dataapiwire.KeyExpired:
		return rule, &Problem{Status: 401, Code: "key_expired", Detail: "This key has expired"}
	case dataapiwire.KeyReplaced:
		return rule, &Problem{Status: 401, Code: "key_replaced", Detail: "This key was renewed and its grace period is over: use the new one"}
	}
	return rule, nil
}

// AllowsAddress says whether the key may be used from this address.
func (r *KeyRule) AllowsAddress(addr string) bool {
	if len(r.nets) == 0 {
		return true
	}
	a, err := netip.ParseAddr(addr)
	if err != nil {
		return false
	}
	a = a.Unmap()
	for _, p := range r.nets {
		if p.IsValid() && p.Contains(a) {
			return true
		}
	}
	return false
}

// AllowsResource says whether the key may read this resource.
func (r *KeyRule) AllowsResource(name string) bool {
	if len(r.Resources) == 0 {
		return true
	}
	for _, x := range r.Resources {
		if x == name {
			return true
		}
	}
	return false
}

// Limiter is each key's rate in this pod: a bucket of a minute's requests, refilled
// continuously. The key's rate is divided by the replicas (each pod counts its own requests:
// approximate, as documented).
type Limiter struct {
	mu      sync.Mutex
	buckets map[int64]*bucket
	now     func() time.Time
}

type bucket struct {
	tokens float64
	at     time.Time
	rate   float64 // per minute
}

// NewLimiter is an empty limiter.
func NewLimiter(now func() time.Time) *Limiter {
	return &Limiter{buckets: map[int64]*bucket{}, now: now}
}

// PerPod is a key's rate per minute in each of `replicas` pods, at least one.
func PerPod(rate, replicas int) float64 {
	if replicas < 1 {
		replicas = 1
	}
	return math.Max(math.Ceil(float64(rate)/float64(replicas)), 1)
}

// Take spends one request of the key; when none is left, the seconds until one is.
func (l *Limiter) Take(keyID int64, perMinute float64) (bool, int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	b := l.buckets[keyID]
	if b == nil {
		b = &bucket{tokens: perMinute, at: now, rate: perMinute}
		l.buckets[keyID] = b
	}
	if b.rate != perMinute { // the key's rate changed: its bucket follows
		b.tokens = math.Min(b.tokens, perMinute)
		b.rate = perMinute
	}
	b.tokens = math.Min(perMinute, b.tokens+now.Sub(b.at).Seconds()*perMinute/60)
	b.at = now
	if b.tokens >= 1 {
		b.tokens--
		return true, 0
	}
	wait := (1 - b.tokens) * 60 / perMinute
	return false, int(math.Ceil(wait))
}

// Usage counts each key's requests per minute until the pod sends them.
type Usage struct {
	mu     sync.Mutex
	counts map[usageKey]*dataapiwire.UsageMinute
}

type usageKey struct {
	key    int64
	minute int64
}

// NewUsage is an empty count.
func NewUsage() *Usage { return &Usage{counts: map[usageKey]*dataapiwire.UsageMinute{}} }

// Record counts a request of a key: its status and the rows it gave.
func (u *Usage) Record(keyID int64, at time.Time, status int, rows int) {
	minute := at.UTC().Truncate(time.Minute)
	u.mu.Lock()
	defer u.mu.Unlock()
	k := usageKey{keyID, minute.Unix()}
	m := u.counts[k]
	if m == nil {
		m = &dataapiwire.UsageMinute{KeyID: keyID, Minute: minute}
		u.counts[k] = m
	}
	m.Requests++
	m.Rows += int64(rows)
	switch {
	case status == http.StatusTooManyRequests:
		m.Throttled++
	case status >= 400:
		m.Errors++
	}
}

// Take is everything counted so far, and the count starts again.
func (u *Usage) Take() []dataapiwire.UsageMinute {
	u.mu.Lock()
	defer u.mu.Unlock()
	out := make([]dataapiwire.UsageMinute, 0, len(u.counts))
	for _, m := range u.counts {
		out = append(out, *m)
	}
	u.counts = map[usageKey]*dataapiwire.UsageMinute{}
	return out
}

// Return puts back what could not be sent, to go with the next report (the oldest are let go
// past the most a report holds).
func (u *Usage) Return(list []dataapiwire.UsageMinute) {
	u.mu.Lock()
	defer u.mu.Unlock()
	for _, m := range list {
		if len(u.counts) >= dataapiwire.MaxUsageMinutes {
			return
		}
		k := usageKey{m.KeyID, m.Minute.Unix()}
		if cur := u.counts[k]; cur != nil {
			cur.Requests += m.Requests
			cur.Rows += m.Rows
			cur.Errors += m.Errors
			cur.Throttled += m.Throttled
		} else {
			mm := m
			u.counts[k] = &mm
		}
	}
}
