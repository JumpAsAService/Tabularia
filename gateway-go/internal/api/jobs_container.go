package api

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// services/container_runs.py run_container (a flow's Container node), with what it needs of
// services/container_registry.py (the tag's digest), services/container_cluster.py (the image
// a Pod names, the pull Secret) and services/kube_jobs.py.

// ── services/container_registry.py ────────────────────────────────────────────

const registryTimeout = 15 * time.Second

var (
	crHost       = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*(:\d{1,5})?$`)
	crRepository = regexp.MustCompile(`^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$`)
	crTag        = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`)
	crDigest     = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	crChallenge  = regexp.MustCompile(`(\w+)="([^"]*)"`)
	crManifests  = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, " +
		"application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
	crMetadata = func() []*net.IPNet {
		var out []*net.IPNet
		for _, n := range []string{"169.254.0.0/16", "fe80::/10", "100.100.100.200/32", "fd00:ec2::254/128", "168.63.129.16/32"} {
			_, ipn, _ := net.ParseCIDR(n)
			out = append(out, ipn)
		}
		return out
	}()
)

// registryError is RegistryError.
type registryError struct{ msg string }

func (e *registryError) Error() string { return e.msg }

type registryRow struct {
	ID                                                        int64
	Name, Host, Protocol, TLS, CACert, Username, Repositories string
	PasswordEnc                                               *string
	Enabled                                                   bool
}

func loadRegistry(ctx context.Context, q perm.Querier, id int64) (*registryRow, error) {
	var r registryRow
	err := q.QueryRow(ctx, `SELECT id, name, host, protocol, tls, ca_cert, username, password_enc, repositories, enabled
		FROM container_registries WHERE id = $1`, id).Scan(&r.ID, &r.Name, &r.Host, &r.Protocol, &r.TLS, &r.CACert, &r.Username, &r.PasswordEnc,
		&r.Repositories, &r.Enabled)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &r, err
}

func crAPIHost(host string) string {
	if host == "docker.io" || host == "index.docker.io" {
		return "registry-1.docker.io"
	}
	return host
}

// crGuard is _guard: an address resolving to the cloud metadata endpoints refused.
func crGuard(ctx context.Context, host string) error {
	name := host
	if strings.Count(host, ":") == 1 {
		name = host[:strings.LastIndex(host, ":")]
	}
	addrs, err := net.DefaultResolver.LookupIPAddr(ctx, name)
	if err != nil {
		return &registryError{fmt.Sprintf("The address %s does not resolve: check its name", host)}
	}
	for _, a := range addrs {
		for _, n := range crMetadata {
			if n.Contains(a.IP) {
				return &registryError{fmt.Sprintf("%s resolves to a cloud metadata address, which is never a registry", host)}
			}
		}
	}
	return nil
}

type crSession struct {
	r        *registryRow
	password string
	base     string
	client   *http.Client
	scheme   string
	params   map[string]string
	tokens   map[string]string
}

func newCRSession(r *registryRow, password string) (*crSession, error) {
	t := http.DefaultTransport.(*http.Transport).Clone()
	switch {
	case r.Protocol == "http" || r.TLS == "none":
		t.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // the administrator's choice
	case r.TLS == "ca":
		if !strings.Contains(r.CACert, "BEGIN CERTIFICATE") {
			return nil, &registryError{"The authority's certificate is missing: paste it in PEM (-----BEGIN CERTIFICATE-----)"}
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM([]byte(r.CACert)) {
			return nil, &registryError{"The authority's certificate cannot be read: paste it in PEM"}
		}
		t.TLSClientConfig = &tls.Config{RootCAs: pool}
	}
	return &crSession{r: r, password: password, base: r.Protocol + "://" + crAPIHost(r.Host), client: &http.Client{Timeout: registryTimeout,
		Transport: t}, tokens: map[string]string{}, params: map[string]string{}}, nil
}

func (s *crSession) explain(err error) error {
	var ue *url.Error
	if errors.As(err, &ue) && ue.Timeout() {
		return &registryError{fmt.Sprintf("%s did not answer within %d s", s.r.Host, int(registryTimeout.Seconds()))}
	}
	return &registryError{fmt.Sprintf("The gateway cannot reach %s: %s", s.r.Host, runes(err.Error(), 200))}
}

func (s *crSession) do(ctx context.Context, method, target string, header map[string]string, basic bool) (*http.Response, []byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, target, nil)
	if err != nil {
		return nil, nil, err
	}
	for k, v := range header {
		req.Header.Set(k, v)
	}
	if basic {
		req.SetBasicAuth(s.r.Username, s.password)
	}
	res, err := s.client.Do(req)
	if err != nil {
		return nil, nil, s.explain(err)
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(res.Body)
	return res, body, nil
}

// ping is _Session.ping.
func (s *crSession) ping(ctx context.Context) error {
	res, body, err := s.do(ctx, http.MethodGet, s.base+"/v2/", nil, false)
	if err != nil {
		return err
	}
	if res.StatusCode == 200 {
		s.scheme = "basic"
		if s.r.Username == "" {
			s.scheme = "anonymous"
		}
		return nil
	}
	if res.StatusCode == 400 && strings.Contains(string(body), "HTTPS server") {
		return &registryError{"The registry answers in HTTPS: choose HTTPS as the protocol"}
	}
	if res.StatusCode != 401 {
		return &registryError{fmt.Sprintf("%s is not a container registry (it answered %d on /v2/)", s.r.Host, res.StatusCode)}
	}
	scheme, rest, _ := strings.Cut(strings.TrimSpace(res.Header.Get("WWW-Authenticate")), " ")
	s.scheme = strings.ToLower(scheme)
	for _, m := range crChallenge.FindAllStringSubmatch(rest, -1) {
		s.params[strings.ToLower(m[1])] = m[2]
	}
	if s.scheme != "basic" && s.scheme != "bearer" {
		shown := s.scheme
		if shown == "" {
			shown = "none"
		}
		return &registryError{fmt.Sprintf("%s asks for an authentication Tabularia does not speak (%s)", s.r.Host, shown)}
	}
	if s.scheme == "basic" {
		if s.r.Username == "" {
			return &registryError{"The registry asks for a user and a password"}
		}
		check, _, err := s.do(ctx, http.MethodGet, s.base+"/v2/", nil, true)
		if err != nil {
			return err
		}
		if check.StatusCode == 401 || check.StatusCode == 403 {
			return &registryError{"The registry refused the user and the password"}
		}
	}
	return nil
}

// token is _Session._token.
func (s *crSession) token(ctx context.Context, scope string) (string, error) {
	if t, ok := s.tokens[scope]; ok {
		return t, nil
	}
	realm := s.params["realm"]
	if !strings.HasPrefix(realm, "https://") && !strings.HasPrefix(realm, "http://") {
		return "", &registryError{fmt.Sprintf("%s names no place to ask for a token", s.r.Host)}
	}
	u, err := url.Parse(realm)
	if err != nil {
		return "", err
	}
	if err := crGuard(ctx, u.Host); err != nil {
		return "", err
	}
	query := url.Values{}
	if s.params["service"] != "" {
		query.Set("service", s.params["service"])
	}
	if scope != "" {
		query.Set("scope", scope)
	}
	target := realm
	if len(query) > 0 {
		sep := "?"
		if strings.Contains(realm, "?") {
			sep = "&"
		}
		target += sep + query.Encode()
	}
	res, body, err := s.do(ctx, http.MethodGet, target, nil, s.r.Username != "")
	if err != nil {
		return "", err
	}
	if res.StatusCode == 401 || res.StatusCode == 403 {
		if s.r.Username != "" {
			return "", &registryError{"The registry refused the user and the password"}
		}
		return "", &registryError{"The registry asks for a user and a password"}
	}
	if res.StatusCode != 200 {
		return "", &registryError{fmt.Sprintf("%s refused a token (%d)", s.r.Host, res.StatusCode)}
	}
	var answer map[string]any
	_ = json.Unmarshal(body, &answer)
	t, _ := answer["token"].(string)
	if t == "" {
		t, _ = answer["access_token"].(string)
	}
	if t == "" {
		return "", &registryError{fmt.Sprintf("%s gave no token", s.r.Host)}
	}
	s.tokens[scope] = t
	return t, nil
}

// get is _Session.get.
func (s *crSession) get(ctx context.Context, method, path, scope string, header map[string]string) (*http.Response, error) {
	if s.scheme == "" {
		if err := s.ping(ctx); err != nil {
			return nil, err
		}
	}
	h := map[string]string{}
	for k, v := range header {
		h[k] = v
	}
	if s.scheme == "bearer" {
		t, err := s.token(ctx, scope)
		if err != nil {
			return nil, err
		}
		h["Authorization"] = "Bearer " + t
	}
	target := path
	if !strings.HasPrefix(path, "http") {
		target = s.base + path
	}
	res, _, err := s.do(ctx, method, target, h, s.scheme == "basic")
	return res, err
}

// crNotVisible is _not_visible.
func crNotVisible(repository string, status int, anonymous bool) error {
	if status == 401 || status == 403 {
		if anonymous {
			return &registryError{fmt.Sprintf("%s is private: the registry asks for a user and a password to read it", repository)}
		}
		return &registryError{fmt.Sprintf("The credentials cannot read %s", repository)}
	}
	return &registryError{fmt.Sprintf("%s is not in the registry, or not visible with these credentials", repository)}
}

// crDigestOf is digest: the digest a tag points to now.
func crDigestOf(ctx context.Context, r *registryRow, password, repository, reference string) (string, error) {
	repo := flowaccess.RepositoryName(r.Host, repository)
	if crDigest.MatchString(reference) {
		return reference, nil
	}
	if !crRepository.MatchString(repo) || !crTag.MatchString(reference) {
		return "", &registryError{fmt.Sprintf("«%s:%s» is not an image name", repository, reference)}
	}
	if len(r.Host) > 253 || !crHost.MatchString(r.Host) {
		return "", &registryError{"The registry's address is a host name, with a port if needed: ghcr.io, registry.acme.internal:5000"}
	}
	s, err := newCRSession(r, password)
	if err != nil {
		return "", err
	}
	if err := crGuard(ctx, crAPIHost(r.Host)); err != nil {
		return "", err
	}
	res, err := s.get(ctx, http.MethodHead, "/v2/"+repo+"/manifests/"+kube.Quote(reference), "repository:"+repo+":pull",
		map[string]string{"Accept": crManifests})
	if err != nil {
		return "", err
	}
	if res.StatusCode == 404 {
		return "", &registryError{fmt.Sprintf("%s has no tag «%s»", repo, reference)}
	}
	if res.StatusCode != 200 {
		return "", crNotVisible(repo, res.StatusCode, r.Username == "")
	}
	found := res.Header.Get("Docker-Content-Digest")
	if !crDigest.MatchString(found) {
		return "", &registryError{fmt.Sprintf("%s did not say which manifest «%s» is", r.Host, reference)}
	}
	return found, nil
}

// ── services/container_cluster.py ─────────────────────────────────────────────

const labelRegistry = "tabularia.io/registry"

// imageRef is image_ref.
func imageRef(r *registryRow, repository, digest string) string {
	host := r.Host
	if host == "docker.io" || host == "index.docker.io" {
		host = "docker.io"
	}
	return fmt.Sprintf("%s/%s@%s", host, flowaccess.RepositoryName(r.Host, repository), digest)
}

// syncPullSecret is sync_pull_secret: the registry's pull Secret in the namespace, its name,
// "" for a registry without credentials.
func syncPullSecret(ctx context.Context, k scriptcfg.Kubernetes, r *registryRow, password, ns string) (string, error) {
	name := fmt.Sprintf("tabularia-registry-%d", r.ID)
	path := "/api/v1/namespaces/" + ns + "/secrets"
	if r.Username == "" {
		_, err := kube.Call(ctx, k, http.MethodDelete, path+"/"+name, nil, false)
		if err != nil && kube.Status(err) != 404 {
			return "", err
		}
		return "", nil
	}
	key := r.Host
	if key == "docker.io" || key == "index.docker.io" {
		key = "https://index.docker.io/v1/"
	}
	auth := base64.StdEncoding.EncodeToString([]byte(r.Username + ":" + password))
	config := pyDumps(pyjson.Object{{Key: "auths", Value: pyjson.Object{{Key: key, Value: pyjson.Object{{Key: "username", Value: r.Username},
		{Key: "password", Value: password}, {Key: "auth", Value: auth}}}}}})
	data := pyjson.Object{{Key: ".dockerconfigjson", Value: base64.StdEncoding.EncodeToString([]byte(config))}}
	meta := pyjson.Object{{Key: "name", Value: name}, {Key: "labels", Value: pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"},
		{Key: labelRegistry, Value: fmt.Sprint(r.ID)}}}}
	manifest := pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "Secret"}, {Key: "type", Value: "kubernetes.io/dockerconfigjson"},
		{Key: "metadata", Value: meta}, {Key: "data", Value: data}}
	_, err := kube.Call(ctx, k, http.MethodPost, path, manifest, false)
	if err == nil {
		return name, nil
	}
	if kube.Status(err) != 409 {
		return "", err
	}
	for attempt := 0; attempt < 3; attempt++ {
		v, err := kube.Call(ctx, k, http.MethodGet, path+"/"+name, nil, false)
		if err != nil {
			return "", err
		}
		current := pyjson.Obj(v)
		if pyjson.Equal(current.Value("data"), data) && pyjson.Equal(current.Value("type"), "kubernetes.io/dockerconfigjson") {
			return name, nil
		}
		m := append(pyjson.Object(nil), meta...).Set("resourceVersion", pyjson.Obj(current.Value("metadata")).Value("resourceVersion"))
		_, err = kube.Call(ctx, k, http.MethodPut, path+"/"+name, append(pyjson.Object(nil), manifest...).Set("metadata", m), false)
		if err == nil {
			return name, nil
		}
		if kube.Status(err) != 409 || attempt == 2 {
			return "", err
		}
	}
	return name, nil
}

// ── services/container_runs.py ────────────────────────────────────────────────

// containerVariables is container_runs._variables.
func (d *Deps) containerVariables(ctx context.Context, r *registryRow, node pyjson.Object) (pyjson.Object, pyjson.Object, pyjson.Object, error) {
	rows, err := d.DB.Query(ctx, `SELECT name, secret, value, value_enc, updated_at FROM container_registry_variables WHERE registry_id = $1`, r.ID)
	if err != nil {
		return nil, nil, nil, err
	}
	type row struct {
		name, value string
		secret      bool
		enc         *string
		updated     *time.Time
	}
	var list []row
	for rows.Next() {
		var x row
		if err := rows.Scan(&x.name, &x.secret, &x.value, &x.enc, &x.updated); err != nil {
			rows.Close()
			return nil, nil, nil, err
		}
		list = append(list, x)
	}
	rows.Close()
	plain, secret, snapshot := pyjson.Object{}, pyjson.Object{}, pyjson.Object{}
	for _, v := range list {
		if v.secret {
			p, err := d.Fernet.Decrypt(deref(v.enc))
			if err != nil {
				return nil, nil, nil, err
			}
			secret = secret.Set(v.name, p)
			var at any
			if v.updated != nil {
				at = pyNaiveISO(*v.updated)
			}
			snapshot = snapshot.Set(v.name, pyjson.Object{{Key: "secret", Value: true}, {Key: "changed_at", Value: at}})
		} else {
			plain = plain.Set(v.name, v.value)
			snapshot = snapshot.Set(v.name, pyjson.Object{{Key: "value", Value: v.value}})
		}
	}
	for _, kv := range flowaccess.VariablesOf(node) {
		if _, taken := snapshot.Get(kv.Key); taken {
			return nil, nil, nil, &scriptFailed{fmt.Sprintf("container «%s»: the variable %s is set by the registry «%s»: give the node's another name",
				flowaccess.ContainerLabel(node), kv.Key, r.Name)}
		}
		plain = plain.Set(kv.Key, kv.Value)
		snapshot = snapshot.Set(kv.Key, pyjson.Object{{Key: "value", Value: kv.Value}, {Key: "node", Value: true}})
	}
	return plain, secret, snapshot, nil
}

// containerResources is container_nodes.resources.
func containerResources(node pyjson.Object) (string, string) {
	d := nodeData(node)
	return pyStripText(pyjson.Str(pyjson.Or(d.Value("cpu"), "1"))), pyStripText(pyjson.Str(pyjson.Or(d.Value("memory"), "1Gi")))
}

// containerJobManifest is container_runs.job_manifest.
func containerJobManifest(js jobSettings, name, image string, node, environment, labels pyjson.Object, timeout int64, secretName,
	pullSecret string) pyjson.Object {
	d := nodeData(node)
	cpu, memory := containerResources(node)
	container := pyjson.Object{{Key: "name", Value: "container"}, {Key: "image", Value: image}, {Key: "imagePullPolicy", Value: "IfNotPresent"},
		{Key: "env", Value: envList(environment)}, {Key: "resources", Value: pyjson.Object{{Key: "requests", Value: pyjson.Object{{Key: "cpu", Value: cpu},
			{Key: "memory", Value: memory}}}, {Key: "limits", Value: pyjson.Object{{Key: "cpu", Value: cpu}, {Key: "memory", Value: memory}}}}},
		{Key: "securityContext", Value: pyjson.Object{{Key: "allowPrivilegeEscalation", Value: false}}}}
	if command := flowaccess.StringsOf(d.Value("command")); len(command) > 0 {
		container = append(container, pyjson.KV{Key: "command", Value: command})
	}
	if args := flowaccess.StringsOf(d.Value("args")); len(args) > 0 {
		container = append(container, pyjson.KV{Key: "args", Value: args})
	}
	if secretName != "" {
		container = append(container, pyjson.KV{Key: "envFrom", Value: []any{pyjson.Object{{Key: "secretRef", Value: pyjson.Object{{Key: "name", Value: secretName}}}}}})
	}
	pod := pyjson.Object{{Key: "restartPolicy", Value: "Never"}, {Key: "automountServiceAccountToken", Value: false},
		{Key: "enableServiceLinks", Value: false}, {Key: "securityContext", Value: pyjson.Object{{Key: "seccompProfile", Value: pyjson.Object{
			{Key: "type", Value: "RuntimeDefault"}}}}}, {Key: "containers", Value: []any{container}}}
	if pullSecret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: pullSecret}}}})
	}
	return jobManifestOf(name, labels, timeout, js.cJobTTL, pod)
}

// runContainer is container_runs.run_container for a flow's Container node.
func (q *queue) runContainer(ctx context.Context, r *scriptNodeRun) (*[2]any, error) {
	d := q.d
	if !q.sc.Containers.Enabled || q.sc.Containers.Namespace == "" {
		return nil, &scriptFailed{"containers are off on this installation (containers.enabled in the Helm chart)"}
	}
	node := r.node
	data := nodeData(node)
	label := flowaccess.ContainerLabel(node)
	if ref := flowaccess.CheckContainer(node, true, d.flowSettings()); ref != nil {
		return nil, &scriptFailed{ref.Detail}
	}
	regID, _ := data.Value("registryId").(int64)
	registry, err := loadRegistry(ctx, d.DB, regID)
	if err != nil {
		return nil, err
	}
	if registry == nil {
		return nil, &scriptFailed{fmt.Sprintf("container «%s»: its container registry no longer exists", label)}
	}
	if !registry.Enabled {
		return nil, &scriptFailed{fmt.Sprintf("container «%s»: the container registry «%s» is not in use", label, registry.Name)}
	}
	repository := flowaccess.RepositoryName(registry.Host, pyStripText(pyjson.Str(data.Value("repository"))))
	acc := flowaccess.New(d.DB, perm.For(d.DB, r.user), r.user, d.flowSettings())
	allowed, err := acc.RepositoryAllowed(ctx, &flowaccess.Registry{ID: registry.ID, Name: registry.Name, Host: registry.Host,
		Repositories: registry.Repositories, Enabled: registry.Enabled}, repository)
	if err != nil {
		return nil, err
	}
	if !allowed {
		return nil, &scriptFailed{fmt.Sprintf("container «%s»: %s may not run %s of «%s»", label, r.user.Email, repository, registry.Name)}
	}
	reference := pyStripText(textOrEmpty(data.Value("tag")))
	if reference == "" {
		reference = "latest"
	}
	password := ""
	if registry.PasswordEnc != nil && *registry.PasswordEnc != "" {
		if password, err = d.Fernet.Decrypt(*registry.PasswordEnc); err != nil {
			return nil, err
		}
	}
	r.io()
	digest, err := crDigestOf(ctx, registry, password, repository, reference)
	var re *registryError
	if errors.As(err, &re) {
		return nil, &scriptFailed{fmt.Sprintf("container «%s»: the image %s:%s could not be resolved: %s", label, repository, reference, re.msg)}
	}
	if err != nil {
		return nil, err
	}
	image := imageRef(registry, repository, digest)
	ns := q.sc.Containers.Namespace
	if r.trial != 0 && q.sc.Containers.TrialNamespace != "" {
		ns = q.sc.Containers.TrialNamespace
	}
	storage, err := loadScriptStorage(ctx, d.DB)
	if err != nil {
		return nil, err
	}
	nodeID := node.Value("id")
	upstream, hasUpstream := incoming(objectList(r.definition.Value("edges")))[nodeKey(nodeID)]["left"]
	hasInput := hasUpstream && upstream != nil
	wantsOutput := scriptHasOutput(r.definition, nodeID)
	if (hasInput || wantsOutput) && !storage.ready() {
		return nil, &scriptFailed{fmt.Sprintf("container «%s»: its input and output go through the scripts bucket, which is not "+
			"configured (Administration → Scripting environments)", label)}
	}
	// a trial asks for the output even with no node after it: the editor previews it
	hasOutput := wantsOutput || (r.trial != 0 && storage.ready())
	usesBucket := hasInput || hasOutput
	stepID := newUUIDText()
	prefix := fmt.Sprintf("container-%d/%s", registry.ID, stepID)
	inKey, outKey := prefix+"/input.parquet", prefix+"/output.parquet"
	engineOutKey := ""
	if hasOutput {
		engineOutKey = "out/" + newHex() + ".parquet"
	}
	plain, secretVars, snapshot, err := d.containerVariables(ctx, registry, node)
	if err != nil {
		return nil, err
	}
	cpu, memory := containerResources(node)
	budget, err := budgetSeconds(data)
	if err != nil {
		return nil, err
	}
	info := pyjson.Object{{Key: "node_id", Value: nodeID}, {Key: "label", Value: label}, {Key: "registry_id", Value: registry.ID},
		{Key: "registry", Value: registry.Name}, {Key: "repository", Value: repository}, {Key: "reference", Value: reference},
		{Key: "digest", Value: digest}, {Key: "image", Value: image}, {Key: "step_id", Value: stepID}, {Key: "input", Value: hasInput},
		{Key: "output", Value: hasOutput}, {Key: "command", Value: flowaccess.StringsOf(data.Value("command"))},
		{Key: "args", Value: flowaccess.StringsOf(data.Value("args"))}, {Key: "cpu", Value: cpu}, {Key: "memory", Value: memory},
		{Key: "attempts", Value: int64(0)}, {Key: "exit_code", Value: nil}, {Key: "variables", Value: snapshot}, {Key: "log", Value: ""},
		{Key: "trial", Value: r.trial != 0}, {Key: "fingerprint", Value: flowaccess.ContainerFingerprint(node)}, {Key: "budget_seconds", Value: budget},
		{Key: "namespace", Value: ns}}
	bucket := d.Config.EngineBucket
	outBucket := ""
	if hasOutput {
		outBucket = bucket
	}
	// without a flow: a container cell of a flow group, a run under the group's
	var flowID any
	flowIDText, flowName := "", ""
	if r.flow != nil {
		flowID, flowIDText, flowName = r.flow.ID, fmt.Sprint(r.flow.ID), r.flow.Name
	}
	childID, err := r.startChild(ctx, d, "container", flowID, registry.Name, outBucket, engineOutKey, info)
	if err != nil {
		return nil, err
	}
	child := &childRun{d: d, id: childID, info: info}
	what := "container «" + label + "»"
	if usesBucket {
		defer func() { d.deleteObjects(context.Background(), storage, []string{inKey, outKey}) }()
	}
	out, err := func() (*[2]any, error) {
		if hasInput {
			if err := q.prepareNodeInput(ctx, r, node, child, storage, inKey, "container_input", "container_run_id", "container", label); err != nil {
				return nil, err
			}
		}
		pullSecret, err := syncPullSecret(ctx, q.sc.Kubernetes, registry, password, ns)
		if err != nil {
			return nil, &scriptFailed{fmt.Sprintf("container «%s»: the registry's pull Secret could not be written: %s", label, err)}
		}
		timeout, retries, delay, err := retrySettings(data)
		if err != nil {
			return nil, err
		}
		bucketURL, inURL, outURL := "", "", ""
		if usesBucket {
			bucketURL = "s3://" + storage.Bucket
		}
		if hasInput {
			inURL = bucketURL + "/" + inKey
		}
		if hasOutput {
			outURL = bucketURL + "/" + outKey
		}
		storageEnv := pyjson.Object{}
		if usesBucket {
			storageEnv = storage.env()
		}
		base := merge(pyjson.Object{{Key: "TABULARIA_INPUT", Value: inURL}, {Key: "TABULARIA_OUTPUT", Value: outURL},
			{Key: "TABULARIA_STEP_ID", Value: stepID}, {Key: "TABULARIA_RUN_ID", Value: fmt.Sprint(r.orchestrationOf(childID))},
			{Key: "TABULARIA_FLOW_ID", Value: flowIDText}, {Key: "TABULARIA_FLOW_NAME", Value: flowName},
			{Key: "TABULARIA_NODE_ID", Value: pyjson.Str(nodeID)}}, storageEnv, plain)
		secret := pyjson.Object{}
		if usesBucket {
			key, err := d.Fernet.Decrypt(storage.SecretEnc)
			if err != nil {
				return nil, err
			}
			secret = pyjson.Object{{Key: "AWS_ACCESS_KEY_ID", Value: storage.AccessKey}, {Key: "AWS_SECRET_ACCESS_KEY", Value: key}}
		}
		secret = merge(secret, secretVars)
		labels := pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"}, {Key: "app.kubernetes.io/component", Value: "container"},
			{Key: labelRegistry, Value: fmt.Sprint(registry.ID)}, {Key: labelContainerRun, Value: fmt.Sprint(childID)},
			{Key: labelOrchestration, Value: fmt.Sprint(r.orchestrationOf(childID))}, {Key: "tabularia.io/stage", Value: "run"}}
		manifest := func(name string, attempt int64) pyjson.Object {
			secretName := ""
			if len(secret) > 0 {
				secretName = name + "-env"
			}
			return containerJobManifest(q.js, name, image, node, merge(base, pyjson.Object{{Key: "TABULARIA_ATTEMPT", Value: fmt.Sprint(attempt)}}),
				labels, timeout, secretName, pullSecret)
		}
		ok, reason, err := q.attempts(ctx, child, "container", what, retries, delay, r.orchestrationOf(childID), hasOutput, storage, outKey, manifest, nil,
			secret, timeout, ns, q.js.cPollSeconds, q.js.cLogMaxBytes)
		if err != nil {
			return nil, err
		}
		if !ok {
			return nil, &scriptFailed{fmt.Sprintf("%s failed: %s", what, reason)}
		}
		return q.importOutput(ctx, child, hasOutput, wantsOutput, storage, outKey, engineOutKey, what,
			fmt.Sprintf("container «%s» did not write its output: the nodes after it read the parquet file the container writes at TABULARIA_OUTPUT", label))
	}()
	return q.closeChild(ctx, child, out, err, what)
}
