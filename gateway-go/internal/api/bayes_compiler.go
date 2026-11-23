package api

// stan-compile's compiler as the gateway builds it (docs/specs/bayes-stan-compile.md, «The state in the list and the
// detail», «A run»): bayescompile.Compiler over the database, the group's current image read as the compile loop reads
// it, the binaries' bytes moved by the engine's /scripts/binary/place and /scripts/binary/delete.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescompile"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// BayesCompiler is the compiler Deps.BayesCompiler takes at start-up: stan-compile's over the database where the
// statistical models can run (Kubernetes with the scripting machinery, the group bayes shipped), nil elsewhere (the
// store keeps NoCompiler, and the page says «compiled the first time it runs»).
func BayesCompiler(d *Deps) bayesmodels.Compiler {
	if _, ok := modelgroups.Get(bayescontract.Group); !ok {
		return nil
	}
	if modelsOff(scriptcfg.Load(config.FromOS())) != "" {
		return nil
	}
	return newBayesCompiler(d)
}

func newBayesCompiler(d *Deps) *bayescompile.Compiler {
	return &bayescompile.Compiler{DB: d.DB, Current: func(ctx context.Context) (*bayescompile.Current, error) {
		return bcCurrent(ctx, d)
	}, Storage: bcEngineStorage{d}, Log: d.Log, Now: jobs.NaiveNow}
}

// bcCurrent is the group's current image and the limits in force, as a round of the loop reads them; nil while the
// group cannot run a Job (disabled, being built, its recipe building no CmdStan).
func bcCurrent(ctx context.Context, d *Deps) (*bayescompile.Current, error) {
	g, ok := modelgroups.Get(bayescontract.Group)
	if !ok {
		return nil, nil
	}
	cmdstan := bayescompile.CmdStanOf(g.Recipe)
	if cmdstan == "" {
		return nil, nil
	}
	img, err := ReadyModelGroupImage(ctx, d.DB, bayescontract.Group)
	var off *ModelGroupUnavailable
	if errors.As(err, &off) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	settings, err := bcSettings(ctx, d.DB)
	if err != nil {
		return nil, err
	}
	limits, err := bayescompile.Resolve(g.Recipe.Compile, g.Recipe.Cache, settings)
	if err != nil {
		return nil, err
	}
	return &bayescompile.Current{Image: bayescompile.Image{CmdStan: cmdstan, Arch: img.Arch, Digest: img.Digest},
		GroupVersion: img.Version, Limits: limits}, nil
}

// bcEngineStorage moves a binary's bytes through the engine, which alone holds Tabularia's storage's key.
type bcEngineStorage struct{ d *Deps }

// Place is the engine's /scripts/binary/place: Tabularia's bayes-binaries/<sha256> copied into the scripts bucket at
// key when its bytes still hash to sha256; bayescompile.ErrAltered when they do not (409 altered) or are gone (404
// missing).
func (s bcEngineStorage) Place(ctx context.Context, sha256, key string) error {
	storage, err := loadScriptStorage(ctx, s.d.DB)
	if err != nil {
		return err
	}
	if !storage.ready() {
		return errors.New("bayes compile: the scripts bucket is not configured")
	}
	cctx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	res, raw, err := s.d.Engine.JSON(cctx, http.MethodPost, "/scripts/binary/place", pyjson.Object{
		{Key: "connection", Value: storage.payload()}, {Key: "sha256", Value: sha256},
		{Key: "bucket", Value: s.d.Config.EngineBucket}, {Key: "target_key", Value: key}})
	if err != nil {
		return fmt.Errorf("bayes compile: the engine did not place the binary: %w", err)
	}
	if res.StatusCode == http.StatusConflict || res.StatusCode == http.StatusNotFound {
		var refusal struct {
			Altered bool `json:"altered"`
			Missing bool `json:"missing"`
		}
		if json.Unmarshal(raw, &refusal) == nil && (refusal.Altered || refusal.Missing) {
			return fmt.Errorf("%w: %s", bayescompile.ErrAltered, engineDetail(raw))
		}
	}
	if res.StatusCode >= 400 {
		return fmt.Errorf("bayes compile: the engine answered %d to the place: %s", res.StatusCode, engineDetail(raw))
	}
	return nil
}

// Delete is the engine's /scripts/binary/delete.
func (s bcEngineStorage) Delete(ctx context.Context, sha256s []string) error {
	return bcDeleteBinaries(ctx, s.d, sha256s)
}
