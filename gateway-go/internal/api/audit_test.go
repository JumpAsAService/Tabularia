package api

import (
	"reflect"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// _detail_for_observer: where and who go, at any depth; numbers under a hidden key stay.
func TestDetailForObserver(t *testing.T) {
	in, err := pyjson.Loads(`{"host": "db", "url": 5, "path": null, "prefix": "tab_x", "to": ["a@b.it"],
		"n": {"sql": "s", "owner": "carla@x.test", "l": ["x@y.z", 3, {"server": {"a": 1}}]}, "prefix2": "p", "who": "@n"}`)
	if err != nil {
		t.Fatal(err)
	}
	want, _ := pyjson.Loads(`{"host": "…", "url": 5, "path": null, "prefix": "tab_…", "to": "…",
		"n": {"sql": "…", "owner": "c***@x.test", "l": ["x***@y.z", 3, {"server": "…"}]}, "prefix2": "p", "who": "***@n"}`)
	if got := detailForObserver(in, "", false); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

// zoneinfo with fold=0: in a gap and in an overlap, the offset before the change.
func TestWallIn(t *testing.T) {
	rome, err := time.LoadLocation("Europe/Rome")
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		wall time.Time
		want string
	}{
		{time.Date(2026, 3, 29, 2, 30, 0, 0, time.UTC), "+01:00"},  // the gap: 02:30+01:00, as Python writes it
		{time.Date(2026, 10, 25, 2, 30, 0, 0, time.UTC), "+02:00"}, // the overlap
		{time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC), "+02:00"},
		{time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC), "+01:00"},
	} {
		got := wallIn(c.wall, rome)
		if got.Format("-07:00") != c.want || got.Hour() != c.wall.Hour() {
			t.Errorf("%v: %v", c.wall, got)
		}
	}
}

func TestSessionHandle(t *testing.T) {
	if h := sessionHandle("abc"); h != "h-ba7816bf8f01cfea" {
		t.Fatal(h)
	}
}
