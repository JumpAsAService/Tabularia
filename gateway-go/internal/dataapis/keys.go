package dataapis

import (
	"errors"
	"fmt"
	"net/netip"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// A key is `tdk_` and 40 random letters and digits, shown once; the database keeps its
// sha-256 and the first 8 characters after `tdk_` to recognise it (dataapiwire: the pods
// check them with the same rules).
const (
	KeyPrefix    = dataapiwire.KeyPrefix
	HintLength   = dataapiwire.HintLength
	MaxKeyName   = 200
	MaxRate      = 100_000
	MaxCIDRs     = 50
	MaxGraceDays = 30
)

// NewKey is a fresh key: the key itself (shown once), its hint and its hash.
func NewKey() (key, hint, hash string, err error) {
	key, err = dataapiwire.Random(KeyPrefix)
	if err != nil {
		return "", "", "", err
	}
	return key, key[len(KeyPrefix) : len(KeyPrefix)+HintLength], HashKey(key), nil
}

// HashKey is what the table keeps of a key: its sha-256, hex.
func HashKey(key string) string { return dataapiwire.Hash(key) }

// WellFormedKey says whether a text can be one of our keys; anything else is not looked up.
func WellFormedKey(key string) bool { return dataapiwire.WellFormed(key, KeyPrefix) }

// The states of a key: answering (active; grace, renewed and still kept for a while), or not
// (replaced, its grace over; expired; revoked).
const (
	KeyActive   = dataapiwire.KeyActive
	KeyGrace    = dataapiwire.KeyGrace
	KeyReplaced = dataapiwire.KeyReplaced
	KeyExpired  = dataapiwire.KeyExpired
	KeyRevoked  = dataapiwire.KeyRevoked
)

// KeyState is a key's state at `now`.
func KeyState(k *Key, now time.Time) string {
	return dataapiwire.KeyStateAt(k.RevokedAt, k.ExpiresAt, k.GraceUntil, now)
}

// Answers says whether a key in this state is let in.
func Answers(state string) bool { return dataapiwire.Answers(state) }

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
