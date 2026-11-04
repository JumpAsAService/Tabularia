package api

// routes/registries.py trial_in_cluster and services/container_cluster.py: can a node of the
// cluster download this image? A Job with the image and a command that does not exist: the node
// downloads it, nothing of it runs.

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/registry"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

const (
	pullMissingCommand = "/tabularia-pull-check-does-not-exist"
	pullFailureSeconds = 20
	pullNoPodSeconds   = 10
	pullRequestWait    = 270
	pullLabel          = "tabularia.io/registry"
)

func init() {
	register("registries_cluster", func(d *Deps) []server.Route {
		return []server.Route{{Method: http.MethodPost, Pattern: "/admin/registries/{registry_id}/cluster-test",
			Handler: scWithBody(d, "/admin/registries/{registry_id}/cluster-test", func(d *Deps, w http.ResponseWriter, r *http.Request,
				p *auth.Principal, body pyd.Body) {
				if scRequire(d, w, r, p, true, regNoRegistryFromToken) {
					registryClusterTest(d, w, r, p, body)
				}
			})}}
	})
}

// pullNamespaces is container_cluster.namespaces.
func pullNamespaces() []string {
	c := scriptcfg.Get().Containers
	var out []string
	for _, n := range []string{c.Namespace, c.TrialNamespace} {
		if n != "" && (len(out) == 0 || out[0] != n) {
			out = append(out, n)
		}
	}
	return out
}

func pullTrialNamespace() string {
	c := scriptcfg.Get().Containers
	if c.TrialNamespace != "" {
		return c.TrialNamespace
	}
	return c.Namespace
}

func dockerHub(host string) bool { return host == "docker.io" || host == "index.docker.io" }

// pullSecret is sync_pull_secret: the registry's pull Secret as its credentials are now; its
// name, or "" without credentials.
func pullSecret(ctx context.Context, reg *regRow, password, namespace string) (string, error) {
	k := kubeAPI()
	name := fmt.Sprintf("tabularia-registry-%d", reg.ID)
	path := "/api/v1/namespaces/" + namespace + "/secrets"
	if reg.Username == "" {
		_, err := kube.Call(ctx, k, http.MethodDelete, path+"/"+name, nil, false)
		if kube.Status(err) == 404 {
			return "", nil
		}
		return "", err
	}
	key := reg.Host
	if dockerHub(reg.Host) {
		key = "https://index.docker.io/v1/"
	}
	cfg, _ := pyjson.Dumps(pyjson.Object{{Key: "auths", Value: pyjson.Object{{Key: key, Value: pyjson.Object{{Key: "username", Value: reg.Username},
		{Key: "password", Value: password}, {Key: "auth", Value: base64.StdEncoding.EncodeToString([]byte(reg.Username + ":" + password))}}}}}},
		pyjson.Options{EnsureASCII: true})
	data := pyjson.Object{{Key: ".dockerconfigjson", Value: base64.StdEncoding.EncodeToString([]byte(cfg))}}
	meta := pyjson.Object{{Key: "name", Value: name}, {Key: "labels", Value: pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"},
		{Key: pullLabel, Value: fmt.Sprint(reg.ID)}}}}
	manifest := pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "Secret"}, {Key: "type", Value: "kubernetes.io/dockerconfigjson"},
		{Key: "metadata", Value: meta}, {Key: "data", Value: data}}
	// kube_jobs.put_secret: created, or replaced as wanted
	_, err := kube.Call(ctx, k, http.MethodPost, path, manifest, false)
	if err == nil {
		return name, nil
	}
	if kube.Status(err) != 409 {
		return "", err
	}
	for attempt := 0; attempt < 3; attempt++ {
		current, err := kube.Call(ctx, k, http.MethodGet, path+"/"+name, nil, false)
		if err != nil {
			return "", err
		}
		cur := kube.Dict(current)
		same, _ := pyjson.Dumps(cur.Value("data"), pyjson.Options{SortKeys: true})
		want, _ := pyjson.Dumps(data, pyjson.Options{SortKeys: true})
		if same == want && cur.Value("type") == "kubernetes.io/dockerconfigjson" {
			return name, nil
		}
		m := append(pyjson.Object{}, meta...)
		m = append(m, pyjson.KV{Key: "resourceVersion", Value: kube.Dict(cur.Value("metadata")).Value("resourceVersion")})
		put := append(pyjson.Object{}, manifest...).Set("metadata", m)
		_, err = kube.Call(ctx, k, http.MethodPut, path+"/"+name, put, false)
		if err == nil {
			return name, nil
		}
		if kube.Status(err) != 409 || attempt == 2 {
			return "", err
		}
	}
	return name, nil
}

// pullCheckJob is _pull_check_job.
func pullCheckJob(name, image, secret string) pyjson.Object {
	pullCheck, _, pullTTL := containersPull()
	labels := pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"}, {Key: "app.kubernetes.io/component", Value: "pull-check"}}
	pod := pyjson.Object{
		{Key: "restartPolicy", Value: "Never"}, {Key: "automountServiceAccountToken", Value: false},
		{Key: "securityContext", Value: pyjson.Object{{Key: "seccompProfile", Value: pyjson.Object{{Key: "type", Value: "RuntimeDefault"}}}}},
		{Key: "containers", Value: []any{pyjson.Object{{Key: "name", Value: "check"}, {Key: "image", Value: image}, {Key: "imagePullPolicy", Value: "Always"},
			{Key: "command", Value: []any{pullMissingCommand}},
			{Key: "resources", Value: pyjson.Object{{Key: "requests", Value: pyjson.Object{{Key: "cpu", Value: "10m"}, {Key: "memory", Value: "16Mi"}}},
				{Key: "limits", Value: pyjson.Object{{Key: "cpu", Value: "100m"}, {Key: "memory", Value: "64Mi"}}}}},
			{Key: "securityContext", Value: pyjson.Object{{Key: "allowPrivilegeEscalation", Value: false}}}}}},
	}
	if secret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: secret}}}})
	}
	return pyjson.Object{{Key: "apiVersion", Value: "batch/v1"}, {Key: "kind", Value: "Job"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: name}, {Key: "labels", Value: labels}}},
		{Key: "spec", Value: pyjson.Object{{Key: "backoffLimit", Value: int64(0)}, {Key: "activeDeadlineSeconds", Value: pullCheck},
			{Key: "ttlSecondsAfterFinished", Value: pullTTL},
			{Key: "template", Value: pyjson.Object{{Key: "metadata", Value: pyjson.Object{{Key: "labels", Value: labels}}}, {Key: "spec", Value: pod}}}}}}
}

// pullPulled is _pulled: the node has the image.
func pullPulled(ctx context.Context, namespace string, pods []any) (bool, error) {
	for _, pod := range pods {
		for _, st := range kube.List(kube.Dict(kube.Dict(pod).Value("status")).Value("containerStatuses")) {
			state := kube.Dict(kube.Dict(st).Value("state"))
			why := kube.Dict(state.Value("waiting")).Value("reason")
			if pyjson.Truthy(state.Value("terminated")) || pyjson.Truthy(state.Value("running")) ||
				why == "CreateContainerError" || why == "RunContainerError" || why == "CreateContainerConfigError" {
				return true, nil
			}
		}
		events, err := kube.JobEvents(ctx, kubeAPI(), namespace, pyjson.Str(kube.Dict(kube.Dict(pod).Value("metadata")).Value("name")))
		if err != nil {
			return false, err
		}
		for _, e := range events {
			if e.Reason == "Pulled" {
				return true, nil
			}
		}
	}
	return false, nil
}

// pullTry is try_pull: {ok, message, image}.
func pullTry(ctx context.Context, reg *regRow, client registry.Registry, repo, reference string) (pyjson.Object, error) {
	pullCheck, pullPoll, _ := containersPull()
	out := func(ok bool, image any, message string) pyjson.Object {
		return pyjson.Object{{Key: "ok", Value: ok}, {Key: "message", Value: message}, {Key: "image", Value: image}}
	}
	digest, err := registry.Digest(ctx, client, repo, reference)
	var re *registry.Error
	if errors.As(err, &re) {
		return out(false, nil, "The gateway could not resolve the image: "+re.Message), nil
	}
	if err != nil {
		return nil, err
	}
	host := reg.Host
	if dockerHub(host) {
		host = "docker.io"
	}
	image := host + "/" + registry.RepositoryName(reg.Host, repo) + "@" + digest
	ns := pullTrialNamespace()
	raw := make([]byte, 16)
	_, _ = rand.Read(raw)
	name := fmt.Sprintf("tabularia-pull-%d-%s", reg.ID, hex.EncodeToString(raw)[:8])
	wait := min(pullCheck+10, pullRequestWait)
	keep := false
	result, err := func() (pyjson.Object, error) {
		secret, err := pullSecret(ctx, reg, client.Password, ns)
		if err != nil {
			return nil, err
		}
		if _, err := kube.CreateJob(ctx, kubeAPI(), ns, pullCheckJob(name, image, secret)); err != nil {
			return nil, err
		}
		started := time.Now()
		var failingSince *time.Time
		poll := time.Duration(pullPoll * float64(time.Second))
		for time.Since(started) < time.Duration(wait)*time.Second {
			time.Sleep(poll)
			pods, err := kube.JobPods(ctx, kubeAPI(), ns, name)
			if err != nil {
				return nil, err
			}
			pulled, err := pullPulled(ctx, ns, pods)
			if err != nil {
				return nil, err
			}
			if pulled {
				return out(true, image, "A node of the cluster downloaded "+image+"; nothing of it was run"), nil
			}
			if len(pods) == 0 && time.Since(started) >= pullNoPodSeconds*time.Second {
				refused, err := kube.Refused(ctx, kubeAPI(), ns, name)
				if err != nil {
					return nil, err
				}
				if refused != "" {
					return out(false, image, "The check could not run: "+refused), nil
				}
			}
			why, err := kube.PullFailure(ctx, kubeAPI(), ns, name)
			if err != nil {
				return nil, err
			}
			now := time.Now()
			switch {
			case why == "":
				failingSince = nil
			case failingSince == nil:
				failingSince = &now
			case now.Sub(*failingSince) >= pullFailureSeconds*time.Second:
				return out(false, image, why), nil
			}
		}
		if pullCheck+10 > wait {
			keep = true
			return out(false, image, fmt.Sprintf("A node is still downloading %s after %d s (a large image, or a busy cluster). It goes on "+
				"downloading it for up to %d s: try again in a few minutes", image, wait, pullCheck)), nil
		}
		return out(false, image, fmt.Sprintf("No node downloaded %s within %d s (a large image, or a busy cluster)", image, pullCheck)), nil
	}()
	var ke *kube.Error
	if errors.As(err, &ke) {
		result, err = out(false, image, "The cluster refused the check: "+ke.Message), nil
	}
	if !keep {
		_ = kube.DeleteJob(ctx, kubeAPI(), ns, name) // a KubeError: its TTL removes it
	}
	return result, err
}

// registryClusterTest is POST /admin/registries/{registry_id}/cluster-test (trial_in_cluster).
func registryClusterTest(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var ps pyd.Params
	ids, _ := pathIntsInto(&ps, r, "registry_id")
	in := ps.Body(body, gatewaymodels.ClusterTestIn)
	if !validated(w, &ps) || !regIsOn(w) {
		return
	}
	if len(pullNamespaces()) == 0 {
		detail(w, http.StatusConflict, "The containers' namespace is not set (containers.enabled in the Helm chart sets CONTAINERS__NAMESPACE)")
		return
	}
	ctx := r.Context()
	reg, ok := regOr404(d, w, ctx, ids[0])
	if !ok {
		return
	}
	name := registry.RepositoryName(reg.Host, in.Get("repository").(string))
	if !registry.Allowed(regPatterns(reg.Repositories), name) {
		detail(w, http.StatusForbidden, fmt.Sprintf("%s is not among the repositories allowed for %s", name, reg.Name))
		return
	}
	client, err := regClient(d, reg)
	if err != nil {
		fail(d, w, "registries: the password", err)
		return
	}
	reference := strings.TrimSpace(in.Get("reference").(string))
	if reference == "" {
		reference = "latest"
	}
	out, err := pullTry(ctx, reg, client, name, reference)
	if err != nil {
		regUnexpected(d, w, "registries: the cluster's test", err)
		return
	}
	okv := out.Value("ok").(bool)
	outcome := "success"
	if !okv {
		outcome = "failure"
	}
	kind := "container_registry"
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "registry.cluster_test", Outcome: outcome, TargetType: &kind,
		TargetID: &reg.ID, TargetLabel: &reg.Name, Detail: pyjson.Object{{Key: "ok", Value: okv}, {Key: "image", Value: out.Value("image")},
			{Key: "message", Value: runeClip(out.Value("message").(string), 500)}}})
	writePy(w, out)
}
