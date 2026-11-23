package bayescompile

import (
	"errors"
	"fmt"
	"sort"
)

var (
	// ErrTooLarge is a binary larger than the whole cache: it is not kept (the row is failed, TooLarge).
	ErrTooLarge = errors.New("bayes compile: the binary is larger than the cache's limit")
	// ErrNoRoom is a cache that cannot be brought under its limit because the rows left are locked (a run is placing
	// them): the evictions found are still returned; the keep waits for the next round.
	ErrNoRoom = errors.New("bayes compile: no room in the cache while binaries are being placed")
)

// Eviction is what the cache gives up: the rows to delete and the objects of Tabularia's storage to delete with them
// (bayes-binaries/<sha256>), only those no row left holds.
type Eviction struct {
	Rows    []Row
	Objects []string // binary sha256s
	Used    int64    // the bytes the ready rows left hold, the incoming binary included
}

// MakeRoom is the room the cache makes for an incoming binary of incoming bytes (0: none, to bring the cache under a
// lowered limit; a limit of 0 empties it): the ready rows evicted until the ready rows' bytes plus incoming fit the
// limit, those of an image that is not currentDigest first, then least recently used (then by id). A row locked by a
// run that is placing it is skipped. incomingSHA256 is the incoming binary's, already in Tabularia's storage: its object
// is never deleted, even when an evicted row of another image holds the same bytes. rows must be every ready row of the
// table (every image), or an object another row holds could be counted as unheld.
func MakeRoom(rows []Row, locked map[int64]bool, currentDigest string, limit, incoming int64, incomingSHA256 string) (Eviction, error) {
	if incoming > limit {
		return Eviction{}, fmt.Errorf("%w: %s over %s", ErrTooLarge, Bytes(incoming), Bytes(limit))
	}
	var ready []Row
	used := incoming
	for _, r := range rows {
		if r.State == StateReady {
			ready = append(ready, r)
			used += r.BinaryBytes
		}
	}
	sort.SliceStable(ready, func(i, j int) bool {
		a, b := ready[i], ready[j]
		if oa, ob := a.ImageDigest != currentDigest, b.ImageDigest != currentDigest; oa != ob {
			return oa
		}
		if !a.LastUsedAt.Equal(b.LastUsedAt) {
			return a.LastUsedAt.Before(b.LastUsedAt)
		}
		return a.ID < b.ID
	})
	var e Eviction
	evicted := map[int64]bool{}
	for _, r := range ready {
		if used <= limit {
			break
		}
		if locked[r.ID] {
			continue
		}
		e.Rows = append(e.Rows, r)
		evicted[r.ID] = true
		used -= r.BinaryBytes
	}
	held := map[string]bool{}
	if incomingSHA256 != "" {
		held[incomingSHA256] = true
	}
	for _, r := range ready {
		if !evicted[r.ID] {
			held[r.BinarySHA256] = true
		}
	}
	gone := map[string]bool{}
	for _, r := range e.Rows {
		if r.BinarySHA256 != "" && !held[r.BinarySHA256] && !gone[r.BinarySHA256] {
			gone[r.BinarySHA256] = true
			e.Objects = append(e.Objects, r.BinarySHA256)
		}
	}
	e.Used = used
	if used > limit {
		return e, fmt.Errorf("%w: %s over %s", ErrNoRoom, Bytes(used), Bytes(limit))
	}
	return e, nil
}
