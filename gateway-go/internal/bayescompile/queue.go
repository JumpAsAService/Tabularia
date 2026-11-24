package bayescompile

import (
	"sort"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
)

// NewRow is the row a program has for an image before anything was asked (state none, no id): what Enqueue starts
// from when the key has no row.
func NewRow(programSHA256 string, img Image, groupVersion int64) (Row, error) {
	key, err := CacheKey(programSHA256, img)
	if err != nil {
		return Row{}, err
	}
	arch := img.Arch
	if arch == "" {
		arch = DefaultArch
	}
	return Row{CacheKey: key, ProgramSHA256: programSHA256, CmdStan: img.CmdStan, ImageDigest: img.Digest, Arch: arch,
		GroupVersion: groupVersion, State: StateNone}, nil
}

// Plan is what Enqueue writes in one transaction, the key's row locked FOR UPDATE and the person's queued rows of the
// same model with it.
type Plan struct {
	Queue     bool  // Row is written (inserted when its ID is 0, else updated): the program is queued
	Row       Row   // the key's row after the request (unchanged when Queue is false)
	Overtaken []Row // rows to delete: the person's older versions of the same model still queued
}

// PlanEnqueue applies «The queue» to a request for the key's row (State none and ID 0 when there is no row):
//
//  1. ready, queued, running: nothing (the cache answers, or a compile is on its way);
//  2. timeout: nothing unless the limit it passed is larger now (MayRetry), whatever the request («Compile now»
//     included); failed because the binary was larger than the cache (too_large): the same rule on the cache's limit,
//     queued again by any request once the limit holds the recorded size, never before (the owner, correction of
//     2026-11-24 from C1); failed for another reason of the compiler: nothing unless the request is «Compile now»
//     (r.Again); failed for the cluster's reason: queued again by any request (a save, a run, «Compile now»);
//  3. otherwise the row becomes queued for r.By, with the request's model and version, its first attempt, queued now;
//  4. overtaken: the person's other rows still queued (not running) for the same model at a lower version, under
//     another key, leave the queue (deleted: their state is none, a run compiles them if a flow pins them). Another
//     model of the same person stays: one compile per person orders them, it does not drop them.
//
// Step 4 is planned whatever the key's row is (ready, queued, running, timeout, failed, none): a newer save always takes
// the older queued version out (the owner, correction of 2026-11-24 from C1), so the store asks Enqueue after every
// save, even when the new version's program is ready or on its way, and deletes Plan.Overtaken even when Plan.Queue is
// false.
//
// others are the candidate rows of step 4 (any the caller read; the rule picks).
func PlanEnqueue(row Row, r bayesmodels.CompileRequest, others []Row, now Limits, at time.Time) Plan {
	p := Plan{Row: row, Overtaken: Overtaken(row, r, others)}
	if row.State == "" {
		row.State = StateNone
	}
	switch row.State {
	case StateNone:
	case StateTimeout:
		if !MayRetry(row, now) {
			return p
		}
	case StateFailed:
		switch {
		case row.Reason == ReasonCluster:
		case row.Reason == ReasonTooLarge:
			if !MayRetry(row, now) {
				return p
			}
		case !r.Again:
			return p
		}
	default: // ready, queued, running
		return p
	}
	if err := move(&row, StateQueued); err != nil {
		return p
	}
	row.RequestedBy, row.ModelID, row.Version = r.By, r.ModelID, r.Version
	row.Attempt, row.QueuedAt = 0, at
	row.Reason, row.Detail, row.JobName = "", "", ""
	row.StartedAt, row.FinishedAt, row.LastUsedAt = time.Time{}, time.Time{}, time.Time{}
	row.BinarySHA256, row.BinaryBytes, row.InfoSHA256, row.Warnings, row.Seconds = "", 0, "", "", 0
	p.Queue, p.Row = true, row
	return p
}

// Overtaken are the rows a request for row's key takes out of the queue: queued (never running), of the same person
// and the same model, at a lower version, under another key.
func Overtaken(row Row, r bayesmodels.CompileRequest, others []Row) []Row {
	if r.By == 0 || r.ModelID == 0 {
		return nil
	}
	var out []Row
	for _, o := range others {
		if o.State == StateQueued && o.RequestedBy == r.By && o.ModelID == r.ModelID && o.Version < r.Version &&
			o.CacheKey != row.CacheKey && (row.ID == 0 || o.ID != row.ID) {
			out = append(out, o)
		}
	}
	return out
}

// NextToStart are the queued rows the loop starts this round: as many as the free places («Compiles at once» less the
// rows running), in queued_at order (then id), skipping a person who has a compile running or already took a place
// this round, so that a person never has two compiles running. rows are the queued and running rows (any image: a
// running compile of an older image still holds its place). A row with no person (requested_by NULL) counts as one
// person of its own.
func NextToStart(rows []Row, slots int) []Row {
	busy := map[int64]bool{}
	free := slots
	var queued []Row
	for _, r := range rows {
		switch r.State {
		case StateRunning:
			free--
			if r.RequestedBy != 0 {
				busy[r.RequestedBy] = true
			}
		case StateQueued:
			queued = append(queued, r)
		}
	}
	if free <= 0 {
		return nil
	}
	sort.SliceStable(queued, func(i, j int) bool {
		if !queued[i].QueuedAt.Equal(queued[j].QueuedAt) {
			return queued[i].QueuedAt.Before(queued[j].QueuedAt)
		}
		return queued[i].ID < queued[j].ID
	})
	var out []Row
	for _, r := range queued {
		if free == 0 {
			break
		}
		if r.RequestedBy != 0 && busy[r.RequestedBy] {
			continue
		}
		if r.RequestedBy != 0 {
			busy[r.RequestedBy] = true
		}
		out = append(out, r)
		free--
	}
	return out
}
