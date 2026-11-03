// Package objects is services/objects.py: who may read which storage object. The requests the
// gateway forwards to the engine name storage keys; every managed key must be readable by the
// person (their own uploads, the datasources of folders they read, the runs they launched or
// whose flow they read, the keys of the flows they read), and every source of a person who is
// not an administrator must be in the engine's bucket and under a managed prefix.
package objects

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
)

// ManagedPrefixes are MANAGED_PREFIXES: everything else cannot be addressed by the clients.
var ManagedPrefixes = []string{"datasets/", "out/", "raw/", "cache/"}

func managed(s string) bool {
	for _, p := range ManagedPrefixes {
		if strings.HasPrefix(s, p) {
			return true
		}
	}
	return false
}

// CollectReadRefs is collect_read_refs: the buckets and the READ keys in a structural position
// (`key`, `input_key`, `bucket` fields) anywhere in a decoded JSON payload.
func CollectReadRefs(payload any) (buckets, keys map[string]bool) {
	buckets, keys = map[string]bool{}, map[string]bool{}
	var walk func(any)
	walk = func(n any) {
		switch x := n.(type) {
		case map[string]any:
			for k, v := range x {
				if s, ok := v.(string); ok {
					if k == "key" || k == "input_key" {
						keys[s] = true
					} else if k == "bucket" {
						buckets[s] = true
					}
				}
				walk(v)
			}
		case []any:
			for _, v := range x {
				walk(v)
			}
		}
	}
	walk(payload)
	return
}

// EnsureReadsPinned is ensure_reads_pinned.
func EnsureReadsPinned(ctx context.Context, c *perm.Checker, payload any, engineBucket string) (*auth.Refusal, error) {
	if admin, err := c.IsAdmin(ctx); err != nil || admin {
		return nil, err
	}
	buckets, keys := CollectReadRefs(payload)
	// the Python sets are iterated in no fixed order; the first refused is reported: sorted
	// here so the answer is stable (the comparison runs one bad reference at a time)
	for _, b := range sortedKeys(buckets) {
		if b != "" && b != engineBucket {
			return &auth.Refusal{Status: http.StatusForbidden, Detail: fmt.Sprintf("bucket not allowed: '%s'", b)}, nil
		}
	}
	for _, k := range sortedKeys(keys) {
		if k != "" && !managed(k) {
			return &auth.Refusal{Status: http.StatusForbidden, Detail: fmt.Sprintf("source outside the managed areas: '%s'", k)}, nil
		}
	}
	return nil, nil
}

// CollectStorageKeys is collect_storage_keys: every string that looks like a managed key.
func CollectStorageKeys(payload any) map[string]bool {
	found := map[string]bool{}
	var walk func(any)
	walk = func(n any) {
		switch x := n.(type) {
		case string:
			if managed(x) {
				found[x] = true
			}
		case map[string]any:
			for _, v := range x {
				walk(v)
			}
		case []any:
			for _, v := range x {
				walk(v)
			}
		}
	}
	walk(payload)
	return found
}

// canReadKey is _can_read_key over the folders `allowed`.
func canReadKey(ctx context.Context, q perm.Querier, user *auth.User, key string, allowed []int64) (bool, error) {
	var one int
	exists := func(sql string, args ...any) (bool, error) {
		err := q.QueryRow(ctx, sql, args...).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return err == nil, err
	}
	if ok, err := exists(`SELECT 1 FROM uploads WHERE parquet_key = $1 AND owner_id = $2 LIMIT 1`, key, user.ID); err != nil || ok {
		return ok, err
	}
	if len(allowed) > 0 {
		if ok, err := exists(`SELECT 1 FROM datasources WHERE key = $1 AND project_id = ANY($2) LIMIT 1`, key, allowed); err != nil || ok {
			return ok, err
		}
	}
	// a run: whoever launched it, or VIEW (RUN) on its flow's folder
	if ok, err := exists(`SELECT 1 FROM runs r WHERE (r.output_key = $1 OR r.input_key = $1) AND (r.launched_by = $2
			OR (cardinality($3::bigint[]) > 0 AND EXISTS (SELECT 1 FROM flows f WHERE f.id = r.flow_id AND f.project_id = ANY($3)))) LIMIT 1`,
		key, user.ID, allowed); err != nil || ok {
		return ok, err
	}
	// `Flow.definition.contains(key)`: a LIKE without escaping, as SQLAlchemy writes it
	if len(allowed) > 0 {
		return exists(`SELECT 1 FROM flows WHERE project_id = ANY($1) AND definition LIKE '%' || $2 || '%' LIMIT 1`, allowed, key)
	}
	return false, nil
}

func ensureKeys(ctx context.Context, c *perm.Checker, q perm.Querier, user *auth.User, keys map[string]bool, needed perm.Capability,
	refuse func(key string) string) (*auth.Refusal, error) {
	if len(keys) == 0 {
		return nil, nil
	}
	if admin, err := c.IsAdmin(ctx); err != nil || admin {
		return nil, err
	}
	allowed, err := c.Projects(ctx, needed)
	if err != nil {
		return nil, err
	}
	ids := perm.Sorted(allowed)
	for _, k := range sortedKeys(keys) {
		ok, err := canReadKey(ctx, q, user, k, ids)
		if err != nil {
			return nil, err
		}
		if !ok {
			return &auth.Refusal{Status: http.StatusForbidden, Detail: refuse(k)}, nil
		}
	}
	return nil, nil
}

// EnsureCanReadKeys is ensure_can_read_keys: a 403 at the first key (in order) not readable.
func EnsureCanReadKeys(ctx context.Context, c *perm.Checker, q perm.Querier, user *auth.User, keys map[string]bool) (*auth.Refusal, error) {
	return ensureKeys(ctx, c, q, user, keys, perm.View, func(k string) string {
		return fmt.Sprintf("You do not have access to the object '%s'", k)
	})
}

// EnsureCanRunKeys is ensure_can_run_keys: RUN instead of VIEW.
func EnsureCanRunKeys(ctx context.Context, c *perm.Checker, q perm.Querier, user *auth.User, keys map[string]bool) (*auth.Refusal, error) {
	return ensureKeys(ctx, c, q, user, keys, perm.Run, func(k string) string {
		return fmt.Sprintf("Missing 'run' permission to run on '%s': RUN on the folder is required", k)
	})
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
