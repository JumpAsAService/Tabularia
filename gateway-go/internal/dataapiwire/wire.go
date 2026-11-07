// Package dataapiwire is what the gateway and the data APIs' pods say to each other
// (docs/specs/data-apis.md, «Runtime»): the manifest a pod asks for at every poll, the usage
// it sends back, and the keys' rules both sides apply. The standard library only: the pods'
// program (gateway-go/dataapi, a module of its own, built with cgo) imports it too.
package dataapiwire

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"math/big"
	"strings"
	"time"
)

// The states a manifest gives: only an active data API answers; a paused one (or a draft)
// answers 503; a suspended one (its creator deactivated, or without MANAGE on its folder)
// too; with the feature switched off in the installation, «disabled».
const (
	StateActive    = "active"
	StatePaused    = "paused"
	StateDraft     = "draft"
	StateSuspended = "suspended"
	StateDisabled  = "disabled"
)

// Manifest is GET /internal/data-apis/{id}/manifest.
type Manifest struct {
	ID    int64  `json:"id"`
	Slug  string `json:"slug"`
	Name  string `json:"name"`
	State string `json:"state"`
	// the version of the definition it serves (the description's version)
	DefinitionVersion int64 `json:"definition_version"`
	// why a suspended data API does not answer: creator_inactive, creator_rights
	Suspended string `json:"suspended,omitempty"`
	// the replicas a key's rate is divided by (each pod counts its own requests)
	Replicas int `json:"replicas"`
	// how often the pods ask again
	PollSeconds int        `json:"poll_seconds"`
	Resources   []Resource `json:"resources"`
	Keys        []Key      `json:"keys"`
	// when the gateway wrote it
	At time.Time `json:"at"`
}

// Filter is a column one may filter on and the operators allowed on it.
type Filter struct {
	Column    string   `json:"column"`
	Operators []string `json:"operators"`
}

// Resource is one path of the data API, and the data it serves now.
type Resource struct {
	Name        string   `json:"name"`
	Columns     []string `json:"columns"`
	Filters     []Filter `json:"filters"`
	DefaultSort []string `json:"default_sort"`
	MaxPage     int      `json:"max_page"`
	// false when the creator may no longer read the datasource (the pods answer 403), or the
	// datasource has no data yet; Unavailable says which
	Available   bool   `json:"available"`
	Unavailable string `json:"unavailable,omitempty"`
	// the data's version, as the answers give it: when the datasource's data was written
	Version string `json:"version,omitempty"`
	// changes whenever the data file does (another object in the storage, another run):
	// what the pods compare to know they must download again
	Fingerprint string `json:"fingerprint,omitempty"`
	// a short-lived link to download the datasource's parquet
	URL string `json:"url,omitempty"`
}

// The reasons a resource is not available.
const (
	UnavailableRights = "datasource_rights" // the creator may no longer read it
	UnavailableNoData = "no_data"           // the datasource has no data file yet
	UnavailableError  = "error"             // the storage did not give a link
)

// Key is a key of the data API as the pods check it: never the key itself, its sha-256.
type Key struct {
	ID            int64      `json:"id"`
	Hint          string     `json:"hint"`
	Hash          string     `json:"hash"`
	ExpiresAt     time.Time  `json:"expires_at"`
	RevokedAt     *time.Time `json:"revoked_at"`
	GraceUntil    *time.Time `json:"grace_until"`
	RatePerMinute int        `json:"rate_per_minute"`
	// the resources it may read; empty: all of them
	Resources []string `json:"resources"`
	// the addresses it is allowed from (CIDR); empty: anywhere
	CIDRs []string `json:"cidrs"`
}

// State is the key's state at `now` (KeyActive, KeyGrace, …).
func (k Key) State(now time.Time) string {
	return KeyStateAt(k.RevokedAt, k.ExpiresAt, k.GraceUntil, now)
}

// Usage is POST /internal/data-apis/{id}/usage: what a pod counted since it last sent, and
// how it is (sent at every poll, with or without counts).
type Usage struct {
	Pod     string        `json:"pod"`
	Minutes []UsageMinute `json:"minutes"`
	// nil from a pod of before piece 3: the counts only
	Status *PodStatus `json:"status,omitempty"`
}

// PodStatus is what a pod says of itself: the page shows it next to what the cluster says.
type PodStatus struct {
	// it answers: an active data API, a fresh manifest, every available resource prepared
	Ready bool `json:"ready"`
	// the state of the manifest it applies ("" before the first one)
	State string `json:"state"`
	// when it last had a manifest; nil: never
	ManifestAt *time.Time `json:"manifest_at"`
	// when it started
	Started   time.Time        `json:"started"`
	Resources []ResourceStatus `json:"resources"`
	// the last thing that went wrong (a manifest not had, a version not prepared), "" when
	// the last attempt of each went well
	Error   string     `json:"error,omitempty"`
	ErrorAt *time.Time `json:"error_at,omitempty"`
}

// ResourceStatus is one resource as a pod serves it.
type ResourceStatus struct {
	Name string `json:"name"`
	// the data version it serves, "" none yet
	Version string `json:"version,omitempty"`
	// the version it is preparing, "" none
	Preparing string `json:"preparing,omitempty"`
	// why the manifest's version did not prepare (tried again later), "" when it did
	Error string `json:"error,omitempty"`
	// the size of the file it serves from, in bytes
	Bytes int64 `json:"bytes,omitempty"`
}

// The most a status may say: names and errors are cut to these.
const (
	MaxStatusResources = 100
	MaxStatusText      = 500
)

// UsageMinute is one key's counts in one minute.
type UsageMinute struct {
	KeyID     int64     `json:"key_id"`
	Minute    time.Time `json:"minute"`
	Requests  int64     `json:"requests"`
	Rows      int64     `json:"rows"`
	Errors    int64     `json:"errors"`
	Throttled int64     `json:"throttled"`
}

// MaxUsageMinutes is the most entries one report may hold.
const MaxUsageMinutes = 10_000

// ── keys ──────────────────────────────────────────────────────────────────────

// A key is `tdk_` and 40 random letters and digits, shown once; the database keeps its
// sha-256 and the first 8 characters after `tdk_` to recognise it. A pod token is `tdp_` and
// 40 more, in the Deployment's Secret; the database keeps its sha-256.
const (
	KeyPrefix      = "tdk_"
	PodTokenPrefix = "tdp_"
	RandomLength   = 40
	HintLength     = 8
)

const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

// Random is `prefix` and 40 random letters and digits.
func Random(prefix string) (string, error) {
	b := make([]byte, RandomLength)
	max := big.NewInt(int64(len(alphabet)))
	for i := range b {
		n, err := rand.Int(rand.Reader, max)
		if err != nil {
			return "", err
		}
		b[i] = alphabet[n.Int64()]
	}
	return prefix + string(b), nil
}

// Hash is what is kept of a key or a token: its sha-256, hex.
func Hash(secret string) string {
	h := sha256.Sum256([]byte(secret))
	return hex.EncodeToString(h[:])
}

// SameHash compares two hashes in constant time.
func SameHash(a, b string) bool {
	return len(a) == len(b) && subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}

// WellFormed says whether a text can be a key (or a token) of this prefix; anything else is
// not looked up.
func WellFormed(secret, prefix string) bool {
	rest, ok := strings.CutPrefix(secret, prefix)
	if !ok || len(rest) != RandomLength {
		return false
	}
	for i := 0; i < len(rest); i++ {
		if !strings.ContainsRune(alphabet, rune(rest[i])) {
			return false
		}
	}
	return true
}

// The states of a key: answering (active; grace, renewed and still kept for a while), or not
// (replaced, its grace over; expired; revoked).
const (
	KeyActive   = "active"
	KeyGrace    = "grace"
	KeyReplaced = "replaced"
	KeyExpired  = "expired"
	KeyRevoked  = "revoked"
)

// KeyStateAt is the state at `now` of a key with these times.
func KeyStateAt(revokedAt *time.Time, expiresAt time.Time, graceUntil *time.Time, now time.Time) string {
	switch {
	case revokedAt != nil:
		return KeyRevoked
	case !expiresAt.After(now):
		return KeyExpired
	case graceUntil != nil && !graceUntil.After(now):
		return KeyReplaced
	case graceUntil != nil:
		return KeyGrace
	}
	return KeyActive
}

// Answers says whether a key in this state is let in.
func Answers(state string) bool { return state == KeyActive || state == KeyGrace }
