package bayescompile

import (
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"
)

func readyRow(id int64, digest string, bytes int64, used time.Time, sha string) Row {
	return Row{ID: id, State: StateReady, ImageDigest: digest, BinaryBytes: bytes, LastUsedAt: used,
		BinarySHA256: strings.Repeat(sha, 64)}
}

// The eviction order: an older image's binaries first, then least recently used; a locked row is skipped; a binary
// larger than the limit is refused; an object still held by a row left (or by the incoming binary) is not deleted.
func TestMakeRoom(t *testing.T) {
	cur, old := imgB.Digest, "sha256:"+strings.Repeat("0", 64)
	at := func(h int) time.Time { return t0.Add(time.Duration(h) * time.Hour) }
	rows := []Row{
		readyRow(1, cur, 3, at(1), "1"), // the oldest use of the current image
		readyRow(2, cur, 3, at(5), "2"),
		readyRow(3, old, 3, at(9), "3"), // an older image, used recently: still first
		readyRow(4, cur, 3, at(2), "4"),
		{ID: 5, State: StateTimeout, ImageDigest: old},                 // holds no bytes: never evicted
		{ID: 6, State: StateQueued, ImageDigest: cur, BinaryBytes: 99}, // not ready: not counted
	}
	// 12 bytes held, limit 10, 3 more: two out (the old image's, then the least recently used)
	e, err := MakeRoom(rows, nil, cur, 10, 3, strings.Repeat("9", 64))
	if err != nil || !reflect.DeepEqual(ids(e.Rows), []int64{3, 1}) || e.Used != 9 {
		t.Fatalf("%v %v used %d", ids(e.Rows), err, e.Used)
	}
	if !reflect.DeepEqual(e.Objects, []string{strings.Repeat("3", 64), strings.Repeat("1", 64)}) {
		t.Fatalf("objects %v", e.Objects)
	}
	// it fits: nothing evicted
	if e, err := MakeRoom(rows, nil, cur, 15, 3, ""); err != nil || len(e.Rows) != 0 || e.Used != 15 {
		t.Fatalf("%v %v", ids(e.Rows), err)
	}
	// a locked row (a run is placing it) is skipped: the next one goes
	e, err = MakeRoom(rows, map[int64]bool{3: true}, cur, 10, 3, "")
	if err != nil || !reflect.DeepEqual(ids(e.Rows), []int64{1, 4}) {
		t.Fatalf("locked: %v %v", ids(e.Rows), err)
	}
	// larger than the whole limit: refused, nothing evicted
	if e, err := MakeRoom(rows, nil, cur, 13, 14, ""); !errors.Is(err, ErrTooLarge) || len(e.Rows) != 0 {
		t.Fatalf("too large: %v %v", ids(e.Rows), err)
	}
	// a lowered limit (no incoming binary) evicts at the next round; a limit of 0 empties the cache
	if e, err := MakeRoom(rows, nil, cur, 6, 0, ""); err != nil || !reflect.DeepEqual(ids(e.Rows), []int64{3, 1}) {
		t.Fatalf("lowered: %v %v", ids(e.Rows), err)
	}
	e, err = MakeRoom(rows, nil, cur, 0, 0, "")
	if err != nil || !reflect.DeepEqual(ids(e.Rows), []int64{3, 1, 4, 2}) || e.Used != 0 || len(e.Objects) != 4 {
		t.Fatalf("emptied: %v %v", ids(e.Rows), err)
	}
	// rows left locked: the evictions found, and ErrNoRoom
	e, err = MakeRoom(rows, map[int64]bool{1: true, 2: true, 4: true}, cur, 0, 0, "")
	if !errors.Is(err, ErrNoRoom) || !reflect.DeepEqual(ids(e.Rows), []int64{3}) {
		t.Fatalf("no room: %v %v", ids(e.Rows), err)
	}
}

// A rebuilt image may compile byte-identical binaries: the object is shared, and goes only with its last row.
func TestMakeRoomSharedObjects(t *testing.T) {
	cur, old := imgB.Digest, "sha256:"+strings.Repeat("0", 64)
	rows := []Row{
		readyRow(1, old, 3, t0, "a"),
		readyRow(2, cur, 3, t0.Add(time.Hour), "a"), // the same bytes under the current image
		readyRow(3, old, 3, t0, "b"),
	}
	e, err := MakeRoom(rows, nil, cur, 6, 3, "")
	if err != nil || !reflect.DeepEqual(ids(e.Rows), []int64{1, 3}) || !reflect.DeepEqual(e.Objects, []string{strings.Repeat("b", 64)}) {
		t.Fatalf("%v %v %v", ids(e.Rows), e.Objects, err)
	}
	// the incoming binary is the evicted row's bytes (already kept under the same object): the object stays
	e, err = MakeRoom(rows[2:], nil, cur, 3, 3, strings.Repeat("b", 64))
	if err != nil || !reflect.DeepEqual(ids(e.Rows), []int64{3}) || len(e.Objects) != 0 {
		t.Fatalf("%v %v %v", ids(e.Rows), e.Objects, err)
	}
}
