package api

// The data APIs' pods talking to the gateway (docs/specs/data-apis.md, «Runtime»): every
// DATA_API__POLL_SECONDS a pod asks for its manifest (the definition, each resource's data
// version and a short-lived link to its parquet, the keys) and sends the usage it counted.
// Both routes take only the pod token of that very data API (dataapis.RotatePodToken: random,
// in the Deployment's Secret, its sha-256 in data_apis), never a person's session; the token
// is good for nothing else. The authority is the creator's, now: checked at every manifest.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// how long the link to a resource's parquet holds: the pods ask again at every poll, and a
// download that has started goes on after it
const dataAPIURLSeconds = 300

// the most a usage report may weigh
const dataAPIUsageMaxBytes = 4 << 20

func init() {
	register("data_apis_pods", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/internal/data-apis/{api_id}/manifest", Handler: dataAPIPod(d, podManifest)},
			{Method: http.MethodPost, Pattern: "/internal/data-apis/{api_id}/usage", Handler: dataAPIPod(d, podUsage)},
		}
	})
}

type podHandler func(d *Deps, w http.ResponseWriter, r *http.Request, a *dataapis.API)

// dataAPIPod lets in only the pod token of the data API in the path.
func dataAPIPod(d *Deps, h podHandler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		refuse := func() {
			w.Header().Set("WWW-Authenticate", "Bearer")
			detail(w, http.StatusUnauthorized, "Not the pod token of this data API")
		}
		id, err := strconv.ParseInt(r.PathValue("api_id"), 10, 64)
		scheme, token, _ := strings.Cut(r.Header.Get("Authorization"), " ")
		if err != nil || !strings.EqualFold(scheme, "bearer") {
			refuse()
			return
		}
		a, err := dataapis.ByPodToken(r.Context(), d.DB, id, strings.TrimSpace(token))
		if err != nil {
			fail(d, w, "data apis: the pod token", err)
			return
		}
		if a == nil {
			refuse()
			return
		}
		h(d, w, r, a)
	})
}

// GET /internal/data-apis/{api_id}/manifest
func podManifest(d *Deps, w http.ResponseWriter, r *http.Request, a *dataapis.API) {
	m, err := buildManifest(r.Context(), d, a)
	if err != nil {
		fail(d, w, "data apis: the manifest", err)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	server.JSON(w, http.StatusOK, m)
}

// buildManifest is what the pods of `a` serve now: nothing but its state when it is not
// active, or not on its creator's authority any longer.
func buildManifest(ctx context.Context, d *Deps, a *dataapis.API) (*dataapiwire.Manifest, error) {
	m := &dataapiwire.Manifest{ID: a.ID, Slug: a.Slug, Name: a.Name, State: a.State, DefinitionVersion: a.CurrentVersion,
		Replicas:    max(a.Runtime.Replicas, 1),
		PollSeconds: d.Config.DataAPI.PollSeconds, Resources: []dataapiwire.Resource{}, Keys: []dataapiwire.Key{}, At: dataapis.Now()}
	if m.PollSeconds <= 0 {
		m.PollSeconds = 20
	}
	if !dataAPIsOn(d) {
		m.State = dataapiwire.StateDisabled
		return m, nil
	}
	if a.State != dataapis.Active {
		return m, nil
	}
	if a.Runtime.Autoscale {
		// a key's rate is divided by the pods running now, as the autoscaler set them, within
		// its bounds: divided by the fewest, the pods scaled out would let through many times it
		wanted, err := dataapis.WantedPods(ctx, d.DB, a.ID)
		if err != nil {
			return nil, err
		}
		m.Replicas = min(max(wanted, a.Runtime.MinReplicas, 1), max(a.Runtime.MaxReplicas, 1))
	}
	creator, err := loadUser(ctx, d.DB, a.CreatorID)
	if err != nil {
		return nil, err
	}
	if creator == nil || !creator.IsActive {
		m.State, m.Suspended = dataapiwire.StateSuspended, "creator_inactive"
		return m, nil
	}
	c := perm.For(d.DB, creator)
	if ok, err := dataAPIMayManage(ctx, c, a.ProjectID); err != nil || !ok {
		m.State, m.Suspended = dataapiwire.StateSuspended, "creator_rights"
		return m, err
	}
	def, err := dataapis.CurrentDefinition(ctx, d.DB, a)
	if err != nil {
		return nil, err
	}
	ids := []int64{}
	for _, res := range def.Resources {
		ids = append(ids, res.Source.ID)
	}
	sources, err := dataapis.Datasources(ctx, d.DB, ids)
	if err != nil {
		return nil, err
	}
	files, err := dataapis.DataFiles(ctx, d.DB, ids)
	if err != nil {
		return nil, err
	}
	for _, res := range def.Resources {
		out := dataapis.WireResource(res)
		ds, file := sources[res.Source.ID], files[res.Source.ID]
		readable := false
		if ds != nil && res.Source.Kind == dataapis.SourceDatasource {
			if readable, err = c.Has(ctx, ds.ProjectID, perm.View); err != nil {
				return nil, err
			}
		}
		switch {
		case !readable:
			out.Unavailable = dataapiwire.UnavailableRights
		case file.Key == "":
			out.Unavailable = dataapiwire.UnavailableNoData
		default:
			link, missing, err := dataAPIPresign(ctx, d, file.Bucket, file.Key)
			switch {
			case err != nil:
				d.Log.Warn("data apis: the link to a resource's data", "data_api", a.ID, "resource", res.Name, "err", err)
				out.Unavailable = dataapiwire.UnavailableError
			case missing:
				out.Unavailable = dataapiwire.UnavailableNoData
			default:
				out.Available, out.URL, out.Version, out.Fingerprint = true, link, file.Version(), file.Fingerprint()
			}
		}
		m.Resources = append(m.Resources, out)
	}
	if m.Keys, err = dataapis.ManifestKeys(ctx, d.DB, a.ID); err != nil {
		return nil, err
	}
	return m, nil
}

// dataAPIPresign is a short-lived link to an object of the storage (dataAPIObject); missing
// when the object is not there.
func dataAPIPresign(ctx context.Context, d *Deps, bucket, key string) (link string, missing bool, err error) {
	link, _, missing, err = dataAPIObject(ctx, d, bucket, key, dataAPIURLSeconds)
	return link, missing, err
}

// POST /internal/data-apis/{api_id}/usage: the counts per key and minute a pod sends with its
// poll, added to data_api_usage, and its status, kept for the page (data_api_pods).
func podUsage(d *Deps, w http.ResponseWriter, r *http.Request, a *dataapis.API) {
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, dataAPIUsageMaxBytes))
	if err != nil {
		detail(w, http.StatusRequestEntityTooLarge, "A usage report is at most 4 MB")
		return
	}
	var in dataapiwire.Usage
	if err := json.Unmarshal(raw, &in); err != nil {
		detail(w, http.StatusUnprocessableEntity, "Not a usage report")
		return
	}
	if len(in.Minutes) > dataapiwire.MaxUsageMinutes {
		detail(w, http.StatusUnprocessableEntity, fmt.Sprintf("A usage report holds at most %d entries", dataapiwire.MaxUsageMinutes))
		return
	}
	ctx := r.Context()
	kept := 0
	err = inTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		if kept, err = dataapis.RecordUsage(ctx, tx, a.ID, in.Minutes); err != nil {
			return err
		}
		if in.Status != nil {
			_, err = dataapis.RecordPodReport(ctx, tx, a.ID, in.Pod, *in.Status)
		}
		return err
	})
	if err != nil {
		fail(d, w, "data apis: the usage", err)
		return
	}
	server.JSON(w, http.StatusOK, map[string]int{"kept": kept, "left_out": len(in.Minutes) - kept})
}
