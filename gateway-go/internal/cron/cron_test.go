package cron

import (
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"
)

// isoformat is datetime.isoformat() of an aware datetime.
func isoformat(l Local) string {
	off := l.Offset()
	sign := "+"
	if off < 0 {
		sign, off = "-", -off
	}
	f := "2006-01-02T15:04:05"
	if l.Wall.Nanosecond()/1000 != 0 {
		f += ".000000"
	}
	h, m := int(off.Hours()), int(off.Minutes())%60
	return l.Wall.Format(f) + sign + twoDigits(h) + ":" + twoDigits(m)
}

func twoDigits(n int) string { return string(rune('0'+n/10)) + string(rune('0'+n%10)) }

// The corpus testdata/croniter.json is what croniter 6.2.4 answered (testdata/gen.py):
// validity, and six fires from each start in five zones.
func TestCroniterCorpus(t *testing.T) {
	raw, err := os.ReadFile("testdata/croniter.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Expr  string   `json:"expr"`
		Valid bool     `json:"valid"`
		Zone  string   `json:"zone"`
		Start string   `json:"start"`
		Next  []string `json:"next"`
	}
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	failed := 0
	for _, c := range cases {
		if IsValid(c.Expr) != c.Valid {
			t.Errorf("%q: valid %v, want %v", c.Expr, !c.Valid, c.Valid)
			failed++
			continue
		}
		if !c.Valid {
			continue
		}
		loc, _ := time.LoadLocation(c.Zone)
		start, _ := time.Parse(time.RFC3339Nano, c.Start)
		s, _ := Parse(c.Expr)
		cur := FromUTC(start, loc)
		var got []string
		for range c.Next {
			n, err := s.Next(cur)
			if err != nil {
				if errors.Is(err, ErrBadDate) {
					got = append(got, "error:CroniterBadDateError")
				} else {
					got = append(got, "error:"+err.Error())
				}
				break
			}
			got = append(got, isoformat(n))
			cur = n
		}
		if strings.Join(got, " ") != strings.Join(c.Next, " ") {
			failed++
			if failed < 25 {
				t.Errorf("%q in %s from %s:\n got  %v\n want %v", c.Expr, c.Zone, c.Start, got, c.Next)
			}
		}
	}
	if failed > 0 {
		t.Errorf("%d of %d cases differ", failed, len(cases))
	}
	t.Logf("%d cases", len(cases))
}
