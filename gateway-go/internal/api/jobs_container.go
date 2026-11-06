package api

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	registryPkg "github.com/JumpAsAService/Tabularia/gateway-go/internal/registry"
)

// services/container_runs.py run_container (a flow's Container node), with what it needs of
// services/container_registry.py (the tag's digest), services/container_cluster.py (the image
// a Pod names, the pull Secret) and services/kube_jobs.py.

// ── services/container_cluster.py ─────────────────────────────────────────────

const labelRegistry = "tabularia.io/registry"

// imageRef is image_ref.
func imageRef(r *regRow, repository, digest string) string {
	host := r.Host
	if host == "docker.io" || host == "index.docker.io" {
		host = "docker.io"
	}
	return fmt.Sprintf("%s/%s@%s", host, flowaccess.RepositoryName(r.Host, repository), digest)
}

// ── services/container_runs.py ────────────────────────────────────────────────

// containerVariables is container_runs._variables.
func (d *Deps) containerVariables(ctx context.Context, r *regRow, node pyjson.Object) (pyjson.Object, pyjson.Object, pyjson.Object, error) {
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
func (q *queue) runContainer(ctx context.Context, r *scriptNodeRun) (*flowresolver.Source, error) {
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
	registry, err := regGet(ctx, d.DB, regID)
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
	digest, err := registryPkg.Digest(ctx, registryPkg.Registry{Host: registry.Host, Protocol: registry.Protocol, TLS: registry.TLS,
		CACert: registry.CACert, Username: registry.Username, Password: password}, repository, reference)
	var re *registryPkg.Error
	if errors.As(err, &re) {
		return nil, &scriptFailed{fmt.Sprintf("container «%s»: the image %s:%s could not be resolved: %s", label, repository, reference, re.Message)}
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
	hasInput, wantsOutput, err := scriptIO(r.definition, nodeID)
	if err != nil {
		return nil, err
	}
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
	out, err := func() (*flowresolver.Source, error) {
		if hasInput {
			if err := q.prepareNodeInput(ctx, r, node, child, storage, inKey, "container_input", "container_run_id", "container", label); err != nil {
				return nil, err
			}
		}
		pullSecret, err := pullSecret(ctx, registry, password, ns)
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
			return containerJobManifest(q.d.jobSettings(), name, image, node, merge(base, pyjson.Object{{Key: "TABULARIA_ATTEMPT", Value: fmt.Sprint(attempt)}}),
				labels, timeout, secretName, pullSecret)
		}
		ok, reason, err := q.attempts(ctx, child, "container", what, retries, delay, r.orchestrationOf(childID), hasOutput, storage, outKey, manifest, nil,
			secret, timeout, ns, q.d.jobSettings().cPollSeconds, q.d.jobSettings().cLogMaxBytes)
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
