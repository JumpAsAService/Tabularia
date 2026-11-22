package api

// What the nodes in runs ask of a model group: the image its Job runs, with its digest, and
// whether a person may use the group (docs/specs/statistical-models.md, «Model groups and their
// images»). The administrator's switches live in models_admin.go.

import (
	"context"
	"fmt"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
)

// ModelGroupImage is the image of a group's version in use, as a model Job runs it.
type ModelGroupImage struct {
	Group         string
	Version       int64  // the number of the version of the group's managed environment
	EnvironmentID int64  // script_environments.id, managed_by = model_group:<Group>
	Image         string // repository@sha256:… : the digest, never a tag
	Digest        string // sha256:…
	Arch          string // the nodes' kubernetes.io/arch the image was built for ("": any)
	RecipeHash    string // of the recipe the image was built from (task.json's recipe_hash)
	RunnerHash    string // of the runner the version was self-tested with
	CPURequest    string
	CPULimit      string
	MemoryRequest string
	MemoryLimit   string
	// MaxTimeoutSeconds is the longest run, MaxRows the most input rows: the administrator's
	// settings, else the recipe's
	MaxTimeoutSeconds int64
	MaxRows           int64
	InternetAccess    bool // false: the Job carries the label that keeps it off the internet
}

// ModelGroupUnavailable is why a group cannot run a step now. Message is the sentence the step
// fails with. Reason: unknown, off, disabled, building, rebuilding or failed.
type ModelGroupUnavailable struct {
	Group, Reason, Message string
}

func (e *ModelGroupUnavailable) Error() string { return e.Message }

// ReadyModelGroupImage is «give me the ready image of group X with its digest»: the version in
// use of the group's managed environment, when the group is enabled, the version is ready (its
// self-test passed) and it was built from the recipe this Tabularia ships. Otherwise a
// *ModelGroupUnavailable, with the sentence of the step; any other error is the database's.
func ReadyModelGroupImage(ctx context.Context, q perm.Querier, group string) (*ModelGroupImage, error) {
	g, ok := modelgroups.Get(group)
	if !ok {
		return nil, &ModelGroupUnavailable{group, "unknown", fmt.Sprintf("there is no model group %q", group)}
	}
	if why := mgDisabledReason(); why != "" {
		return nil, &ModelGroupUnavailable{group, "off", "the statistical models need Kubernetes with the scripting environments configured"}
	}
	s, err := mgLoad(ctx, q, g, false, 5)
	if err != nil {
		return nil, err
	}
	if !s.Enabled {
		return nil, &ModelGroupUnavailable{group, "disabled", fmt.Sprintf("the model group %s is disabled", group)}
	}
	cur := s.current()
	if cur == nil || cur.ImageRef == nil {
		switch s.state() {
		case "failed":
			why := ""
			if s.Versions[0].Error != nil {
				why = ": " + firstLine(*s.Versions[0].Error)
			}
			return nil, &ModelGroupUnavailable{group, "failed", fmt.Sprintf("the model group %s has no working image: its last build failed%s", group, why)}
		case "licence_pending":
			return nil, &ModelGroupUnavailable{group, "licence_pending", fmt.Sprintf("the model group %q waits for an administrator to accept the licence of its weights", group)}
		case "rebuild_needed", "ready":
			return nil, &ModelGroupUnavailable{group, "rebuilding", fmt.Sprintf("the model group %s is being rebuilt for this version of Tabularia", group)}
		}
		if s.Versions != nil && len(s.Versions) > 0 && s.Versions[0].RecipeHash != nil && *s.Versions[0].RecipeHash != g.RecipeHash {
			return nil, &ModelGroupUnavailable{group, "rebuilding", fmt.Sprintf("the model group %s is being rebuilt for this version of Tabularia", group)}
		}
		return nil, &ModelGroupUnavailable{group, "building", fmt.Sprintf("the model group %s is not ready: its image is still being built and checked", group)}
	}
	img := &ModelGroupImage{Group: group, Version: cur.Number, EnvironmentID: s.EnvID, Image: *cur.ImageRef, RecipeHash: *cur.RecipeHash,
		MaxTimeoutSeconds: s.maxSeconds(), MaxRows: s.maxRows(), CPULimit: s.CPULimit, MemoryLimit: s.Memory}
	if _, digest, ok := strings.Cut(*cur.ImageRef, "@"); ok {
		img.Digest = digest
	}
	if cur.RunnerHash != nil {
		img.RunnerHash = *cur.RunnerHash
	}
	if cur.Arch != nil {
		img.Arch = *cur.Arch
	}
	img.CPURequest, img.MemoryRequest = mgHalf(s.CPULimit, true), mgHalf(s.Memory, false)
	return img, nil
}

func firstLine(s string) string {
	line, _, _ := strings.Cut(strings.TrimSpace(s), "\n")
	return line
}

// ModelGroupUsable says whether a person may use a group's nodes: the group is enabled and the
// person is an administrator, or the group is for everyone who can edit flows, or the person is
// in one of the user groups listed. (Editing the flow is the flow's own check.)
func ModelGroupUsable(ctx context.Context, q perm.Querier, c *perm.Checker, userID int64, group string) (bool, error) {
	g, ok := modelgroups.Get(group)
	if !ok {
		return false, nil
	}
	s, err := mgLoad(ctx, q, g, false, 1)
	if err != nil || !s.Enabled {
		return false, err
	}
	if s.Everyone {
		return true, nil
	}
	return scriptnodes.CanUse(ctx, c, q, userID, s.EnvID)
}
