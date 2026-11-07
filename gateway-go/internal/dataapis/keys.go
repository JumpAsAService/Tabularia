package dataapis

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math/big"
	"net/netip"
	"strconv"
	"strings"
	"time"
)

// A key is `tdk_` and 40 random letters and digits, shown once; the database keeps its
// sha-256 and the first 8 characters after `tdk_` to recognise it.
const (
	KeyPrefix    = "tdk_"
	keyRandom    = 40
	HintLength   = 8
	MaxKeyName   = 200
	MaxRate      = 100_000
	MaxCIDRs     = 50
	MaxGraceDays = 30
)

const keyAlphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

// NewKey is a fresh key: the key itself (shown once), its hint and its hash.
func NewKey() (key, hint, hash string, err error) {
	b := make([]byte, keyRandom)
	max := big.NewInt(int64(len(keyAlphabet)))
	for i := range b {
		n, err := rand.Int(rand.Reader, max)
		if err != nil {
			return "", "", "", err
		}
		b[i] = keyAlphabet[n.Int64()]
	}
	key = KeyPrefix + string(b)
	return key, string(b[:HintLength]), HashKey(key), nil
}

// HashKey is what the table keeps of a key: its sha-256, hex.
func HashKey(key string) string {
	h := sha256.Sum256([]byte(key))
	return hex.EncodeToString(h[:])
}

// WellFormedKey says whether a text can be one of our keys; anything else is not looked up.
func WellFormedKey(key string) bool {
	rest, ok := strings.CutPrefix(key, KeyPrefix)
	if !ok || len(rest) != keyRandom {
		return false
	}
	for i := 0; i < len(rest); i++ {
		if !strings.ContainsRune(keyAlphabet, rune(rest[i])) {
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

// KeyState is a key's state at `now`.
func KeyState(k *Key, now time.Time) string {
	switch {
	case k.RevokedAt != nil:
		return KeyRevoked
	case !k.ExpiresAt.After(now):
		return KeyExpired
	case k.GraceUntil != nil && !k.GraceUntil.After(now):
		return KeyReplaced
	case k.GraceUntil != nil:
		return KeyGrace
	}
	return KeyActive
}

// Answers says whether a key in this state is let in.
func Answers(state string) bool { return state == KeyActive || state == KeyGrace }

// KeyExpiry reads a key's expiry: a date and time (RFC 3339), in the future, within the
// longest life of a key (and a day of margin, for the end of the last day in any time zone).
func KeyExpiry(text string, maxDays int, now time.Time) (time.Time, error) {
	if strings.TrimSpace(text) == "" {
		return time.Time{}, errors.New("a key needs an expiry")
	}
	t, err := time.Parse(time.RFC3339, strings.TrimSpace(text))
	if err != nil {
		return time.Time{}, errors.New("the expiry is a date and time (RFC 3339)")
	}
	t = t.UTC().Truncate(time.Second)
	if !t.After(now.Add(time.Minute)) {
		return time.Time{}, errors.New("the expiry is in the past")
	}
	if t.After(now.Add(time.Duration(maxDays+1) * 24 * time.Hour)) {
		return time.Time{}, errors.New("the expiry is further than the longest life of a key (" + strconv.Itoa(maxDays) + " days)")
	}
	return t, nil
}

// CIDRs checks the source addresses a key is allowed from: addresses or ranges, IPv4 or
// IPv6; an address alone is a range of one. Returned as ranges, without duplicates.
func CIDRs(list []string) ([]string, error) {
	if len(list) > MaxCIDRs {
		return nil, fmt.Errorf("at most %d address ranges", MaxCIDRs)
	}
	out := []string{}
	seen := map[string]bool{}
	for _, raw := range list {
		s := strings.TrimSpace(raw)
		if s == "" {
			continue
		}
		var p netip.Prefix
		if strings.Contains(s, "/") {
			parsed, err := netip.ParsePrefix(s)
			if err != nil {
				return nil, fmt.Errorf("«%s» is not an address range (such as 203.0.113.0/24)", s)
			}
			p = parsed.Masked()
		} else {
			a, err := netip.ParseAddr(s)
			if err != nil {
				return nil, fmt.Errorf("«%s» is not an address", s)
			}
			p = netip.PrefixFrom(a, a.BitLen())
		}
		if !seen[p.String()] {
			seen[p.String()] = true
			out = append(out, p.String())
		}
	}
	return out, nil
}
