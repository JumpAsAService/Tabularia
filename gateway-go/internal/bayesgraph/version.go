package bayesgraph

import (
	"encoding/json"
	"fmt"
)

// FormatVersion is the format this code writes and reads. A later format comes with an upgrade
// function vN -> vN+1 (pure, tested on the corpus of the old version); a version newer than
// this is refused by Decode. A saved version is never rewritten in place: the upgrade runs on
// read.
const FormatVersion = 1

// An upgrade rewrites a parsed document (maps, slices, strings, json.Number, bool, nil) from
// one format version to the next. It must not modify its argument.
type upgradeFunc func(doc map[string]any) (map[string]any, error)

// upgrades[v] upgrades version v to v+1. There is none yet: version 1 is the first. This is
// the placeholder the next format fills in.
var upgrades = map[int]upgradeFunc{}

// versionOf reads format_version: an integer of at least 1.
func versionOf(doc map[string]any) (int, error) {
	raw, ok := doc["format_version"]
	if !ok {
		return 0, fmt.Errorf("format_version is missing")
	}
	n, ok := raw.(json.Number)
	if !ok {
		return 0, fmt.Errorf("format_version must be an integer")
	}
	v, err := n.Int64()
	if err != nil || v < 1 || v > 1<<31 {
		return 0, fmt.Errorf("format_version must be an integer of at least 1")
	}
	return int(v), nil
}

// upgrade applies table[from], table[from+1], ... until to, setting format_version
// at each step. A version with no upgrade function is an error.
func upgrade(doc map[string]any, from, to int, table map[int]upgradeFunc) (map[string]any, error) {
	for v := from; v < to; v++ {
		fn, ok := table[v]
		if !ok {
			return nil, fmt.Errorf("no upgrade from version %d", v)
		}
		next, err := fn(doc)
		if err != nil {
			return nil, err
		}
		next["format_version"] = json.Number(fmt.Sprint(v + 1))
		doc = next
	}
	return doc, nil
}
