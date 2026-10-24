{{/*
Shared helpers.

The important one is "tabularia.celeryDeployment": the run worker, the preview
worker and beat differ only by command, queue and sizing, so they share a single
deployment template. Keeping them identical is deliberate — they run the same
image and must see the same configuration.
*/}}

{{- define "tabularia.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tabularia.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "tabularia.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "tabularia.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "tabularia.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "tabularia.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "tabularia.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Name of the Secret every pod reads from. */}}
{{- define "tabularia.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "tabularia.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "tabularia.configMapName" -}}
{{- printf "%s-config" (include "tabularia.fullname" .) -}}
{{- end -}}

{{/* Fully qualified image reference; registry is optional. */}}
{{- define "tabularia.image" -}}
{{- $img := index .root.Values.image .component -}}
{{- if .root.Values.image.registry -}}
{{- printf "%s/%s:%s" .root.Values.image.registry $img.repository $img.tag -}}
{{- else -}}
{{- printf "%s:%s" $img.repository $img.tag -}}
{{- end -}}
{{- end -}}

{{/*
Every pod reads the same ConfigMap and Secret, exactly as docker-compose feeds
the same .env to every service. It keeps the deployment honest: SECURITY__FERNET_KEY
in particular MUST be identical everywhere or stored database credentials become
undecryptable.
*/}}
{{/*
A value for an environment variable, quoted. Helm reads every number in a values
file as a float, and `quote` prints a float of a million or more in exponent form
("5e+06"), which the application refuses as an integer — the engine would not
start. Whole numbers are printed in full; everything else as it is.
*/}}
{{- define "tabularia.env" -}}
{{- if and (kindIs "float64" .) (eq (float64 (int64 .)) .) -}}
{{- int64 . | toString | quote -}}
{{- else -}}
{{- toString . | quote -}}
{{- end -}}
{{- end -}}

{{/*
Secrets a role must NOT hold. Every pod reads the one Secret (envFrom); these
`env` entries, which win over envFrom, empty what the role has no use for — as
the production compose file does.
  gateway-only: the engine roles run users' SQL and connections, and whoever took
  one over must not find the key that signs sessions or the metadata password.
  engine-only: the gateway needs neither the storage nor the warehouse keys.
*/}}
{{- define "tabularia.withoutGatewaySecrets" -}}
{{- range list "JWT__SECRET" "DB__PASSWORD" "DB__URL" "AUTH__ADMIN_PASSWORD" "OIDC__CLIENT_SECRET" "AI__SECRET_KEY" "OPENLINEAGE__API_KEY" }}
- name: {{ . }}
  value: ""
{{- end }}
{{- end -}}

{{- define "tabularia.withoutEngineSecrets" -}}
{{- range list "STORAGE__ACCESS_KEY" "STORAGE__SECRET_KEY" "BACKUP__ACCESS_KEY" "BACKUP__SECRET_KEY" "REDIS__PASSWORD" "CLICKHOUSE_EXTERNAL__PASSWORD" "CLICKHOUSE_EXTERNAL__AI_PASSWORD" "BIGQUERY__CREDENTIALS_B64" }}
- name: {{ . }}
  value: ""
{{- end }}
{{- end -}}

{{/*
Rolls the pods when the chart's Secret changes, as checksum/config does for the
ConfigMap: environment variables are read when a container starts, so without it a
new key or password in the values stays unused until something restarts the pods.
A Secret of your own (existingSecret) is outside the chart: restart the pods after
changing it.
*/}}
{{- define "tabularia.secretChecksum" -}}
{{- if not .Values.existingSecret }}
checksum/secret: {{ include (print .Template.BasePath "/secret.yaml") . | sha256sum }}
{{- end }}
{{- end -}}

{{- define "tabularia.envFrom" -}}
- configMapRef:
    name: {{ include "tabularia.configMapName" . }}
- secretRef:
    name: {{ include "tabularia.secretName" . }}
{{- end -}}

{{- define "tabularia.imagePullSecrets" -}}
{{- with .Values.image.pullSecrets }}
imagePullSecrets:
{{ toYaml . | indent 2 }}
{{- end }}
{{- end -}}

{{/*
Deployment shared by the three Celery roles.
Call with: (dict "root" $ "role" "worker" "cfg" .Values.worker "command" (list ...))
*/}}
{{- define "tabularia.celeryDeployment" -}}
{{- $root := .root -}}
{{- $cfg := .cfg -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "tabularia.fullname" $root }}-{{ .role }}
  labels:
    {{- include "tabularia.labels" $root | nindent 4 }}
    app.kubernetes.io/component: {{ .role }}
spec:
  {{- if .singleton }}
  # Singleton: see README → Why beat is a singleton.
  replicas: 1
  strategy:
    type: Recreate
  {{- else }}
  {{- if not $cfg.autoscaling.enabled }}
  replicas: {{ $cfg.replicaCount }}
  {{- end }}
  {{- end }}
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ include "tabularia.name" $root }}
      app.kubernetes.io/instance: {{ $root.Release.Name }}
      app.kubernetes.io/component: {{ .role }}
  template:
    metadata:
      annotations:
        # Roll the pods when configuration changes; without this a values-only
        # upgrade leaves the old settings running.
        checksum/config: {{ include (print $root.Template.BasePath "/configmap.yaml") $root | sha256sum }}
        {{- include "tabularia.secretChecksum" $root | nindent 8 }}
        {{- with $cfg.podAnnotations }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      labels:
        {{- include "tabularia.labels" $root | nindent 8 }}
        app.kubernetes.io/component: {{ .role }}
    spec:
      {{- include "tabularia.imagePullSecrets" $root | nindent 6 }}
      serviceAccountName: {{ include "tabularia.serviceAccountName" $root }}
      automountServiceAccountToken: {{ $root.Values.serviceAccount.automountToken }}
      securityContext:
        {{- toYaml $root.Values.podSecurityContext | nindent 8 }}
      # Long-running tasks: let Celery finish the task in flight instead of
      # killing it mid-write during a rolling update.
      terminationGracePeriodSeconds: {{ $cfg.terminationGracePeriodSeconds | default 60 }}
      containers:
        - name: {{ .role }}
          image: {{ include "tabularia.image" (dict "root" $root "component" "backend") }}
          imagePullPolicy: {{ $root.Values.image.pullPolicy }}
          securityContext:
            {{- toYaml $root.Values.containerSecurityContext | nindent 12 }}
          command:
            {{- toYaml .command | nindent 12 }}
          envFrom:
            {{- include "tabularia.envFrom" $root | nindent 12 }}
          env:
            {{- if $cfg.concurrency }}
            - name: CELERY__WORKER_CONCURRENCY
              value: {{ $cfg.concurrency | quote }}
            {{- end }}
            # glibc opens one malloc arena per thread; with Polars/Arrow thread
            # pools that inflates resident memory. Capping the arenas flattens it.
            - name: MALLOC_ARENA_MAX
              value: "2"
            # past the size of /tmp the kubelet evicts the pod at once: DuckDB spills within it
            - name: TMP_SIZE_LIMIT
              value: {{ $cfg.tmpSizeLimit | quote }}
            {{- include "tabularia.withoutGatewaySecrets" $root | nindent 12 }}
          resources:
            {{- toYaml $cfg.resources | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        # The image runs with a read-only root filesystem, so every scratch write
        # goes here. Size it for the largest dataset this role handles.
        - name: tmp
          emptyDir:
            sizeLimit: {{ $cfg.tmpSizeLimit }}
      {{- with $cfg.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cfg.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cfg.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
{{- end -}}

{{/*
Admin → Workers on Kubernetes (adminScaling.enabled): where the gateway finds the two
worker Deployments, their autoscalers (none, a CPU HPA or a KEDA ScaledObject, by
the same name) and the highest maxReplicas the panel may set for each.
*/}}
{{- define "tabularia.scalingEnv" -}}
{{- $full := include "tabularia.fullname" . -}}
- name: KUBERNETES__SCALING
  value: "true"
{{- range $kind, $v := dict "run" (dict "role" "worker" "cfg" .Values.worker) "preview" (dict "role" "preview-worker" "cfg" .Values.previewWorker) }}
{{- $name := printf "%s-%s" $full $v.role }}
{{- $a := $v.cfg.autoscaling }}
- name: KUBERNETES__{{ upper $kind }}_WORKER_DEPLOYMENT
  value: {{ $name | quote }}
- name: KUBERNETES__{{ upper $kind }}_WORKER_AUTOSCALER
  value: {{ ternary (printf "%s/%s" (ternary "scaledobject" "hpa" (dig "keda" "enabled" false $a)) $name) "" (and $a.enabled true) | quote }}
- name: KUBERNETES__{{ upper $kind }}_WORKER_MAX_REPLICAS
  value: {{ $a.panelMaxReplicas | default $a.maxReplicas | quote }}
{{- end }}
{{- end -}}

{{/*
Admin → Scripting environments (scripts.enabled): the two namespaces, and what the
gateway needs to build the images there.
*/}}
{{- define "tabularia.scriptsNamespace" -}}
{{- .Values.scripts.namespace | default (printf "%s-scripts" .Release.Namespace) -}}
{{- end -}}

{{- define "tabularia.scriptBuildsNamespace" -}}
{{- .Values.scripts.buildNamespace | default (printf "%s-script-builds" .Release.Namespace) -}}
{{- end -}}

{{- define "tabularia.scriptTrialsNamespace" -}}
{{- .Values.scripts.trialNamespace | default (printf "%s-scripts-dev" .Release.Namespace) -}}
{{- end -}}

{{- define "tabularia.containersNamespace" -}}
{{- .Values.containers.namespace | default (printf "%s-containers" .Release.Namespace) -}}
{{- end -}}

{{- define "tabularia.containerTrialsNamespace" -}}
{{- .Values.containers.trialNamespace | default (printf "%s-containers-dev" .Release.Namespace) -}}
{{- end -}}

{{- define "tabularia.containersEnv" -}}
- name: CONTAINERS__NAMESPACE
  value: {{ include "tabularia.containersNamespace" . | quote }}
- name: CONTAINERS__TRIAL_NAMESPACE
  value: {{ include "tabularia.containerTrialsNamespace" . | quote }}
- name: CONTAINERS__MAX_CPU
  value: {{ .Values.containers.limits.cpu | quote }}
- name: CONTAINERS__MAX_MEMORY
  value: {{ .Values.containers.limits.memory | quote }}
- name: CONTAINERS__MAX_TIMEOUT_SECONDS
  value: {{ .Values.containers.limits.timeoutSeconds | quote }}
{{- end -}}

{{- define "tabularia.scriptsEnv" -}}
{{- $s := .Values.scripts -}}
- name: SCRIPTS__ENABLED
  value: "true"
- name: SCRIPTS__NAMESPACE
  value: {{ include "tabularia.scriptsNamespace" . | quote }}
- name: SCRIPTS__BUILD_NAMESPACE
  value: {{ include "tabularia.scriptBuildsNamespace" . | quote }}
- name: SCRIPTS__TRIAL_NAMESPACE
  value: {{ include "tabularia.scriptTrialsNamespace" . | quote }}
- name: SCRIPTS__REGISTRY
  value: {{ required "scripts.registry.repository is required with scripts.enabled" $s.registry.repository | quote }}
- name: SCRIPTS__REGISTRY_INSECURE
  value: {{ $s.registry.insecure | quote }}
- name: SCRIPTS__REGISTRY_PUSH_SECRET
  value: {{ $s.registry.pushSecret | quote }}
- name: SCRIPTS__REGISTRY_PULL_SECRET
  value: {{ $s.registry.pullSecret | quote }}
{{- if $s.registry.caCert }}
- name: SCRIPTS__REGISTRY_CA_CONFIGMAP
  value: {{ printf "%s-registry-ca" (include "tabularia.fullname" .) | quote }}
{{- end }}
- name: SCRIPTS__BUILDER_IMAGE
  value: {{ $s.builderImage | quote }}
{{- with $s.baseImages }}
- name: SCRIPTS__BASE_IMAGES
  value: {{ toJson . | quote }}
{{- end }}
- name: SCRIPTS__PYPI_INDEX_URL
  value: {{ $s.pypiIndexUrl | quote }}
{{- with $s.cranUrl }}
- name: SCRIPTS__CRAN_URL
  value: {{ . | quote }}
{{- end }}
{{- end -}}

{{/*
The subjects of the Roles that let Tabularia talk to the Kubernetes API: the
gateway's account and the orchestrator's (scaling-rbac.yaml).
*/}}
{{- define "tabularia.kubeApiSubjects" -}}
- kind: ServiceAccount
  name: {{ include "tabularia.fullname" . }}-scaler
  namespace: {{ .Release.Namespace }}
- kind: ServiceAccount
  name: {{ include "tabularia.fullname" . }}-orchestrator
  namespace: {{ .Release.Namespace }}
{{- end }}

{{/*
Whether an address range given to a network policy holds the cloud metadata address,
which must then be cut out of it: "true", or nothing. The cut is valid only inside a
wider range (the API refuses an `except` that is not), so a range beside it, such as a
node-local DNS cache on 169.254.20.10/32, is left whole; the metadata address listed
alone is refused. IPv6: the ranges that hold fd00:ec2::254.
*/}}
{{- define "tabularia.coversMetadata" -}}
{{- if contains ":" . -}}
{{- if has . (list "::/0" "fc00::/7" "fd00::/8") }}true{{ end -}}
{{- else -}}
{{- $parts := splitList "/" . -}}
{{- $p := 32 -}}
{{- if eq (len $parts) 2 }}{{ $p = atoi (index $parts 1) }}{{ end -}}
{{- $o := splitList "." (index $parts 0) -}}
{{- if ne (len $o) 4 }}{{ fail (printf "%s is not an IPv4 address range" .) }}{{ end -}}
{{- $a := atoi (index $o 0) }}{{ $b := atoi (index $o 1) }}{{ $c := atoi (index $o 2) }}{{ $d := atoi (index $o 3) -}}
{{- $pow := list 1 2 4 8 16 32 64 128 256 512 1024 2048 4096 8192 16384 32768 65536 -}}
{{- if ge $p 32 -}}
{{- if and (eq $a 169) (eq $b 254) (eq $c 169) (eq $d 254) }}{{ fail "a network policy range must not be the cloud metadata address 169.254.169.254" }}{{ end -}}
{{- else if lt $p 16 -}}
{{- $s := index $pow (sub 16 $p) -}}
{{- if eq (div (add (mul $a 256) $b) $s) (div 43518 $s) }}true{{ end -}}
{{- else if le $p 24 -}}
{{- $s := index $pow (sub 24 $p) -}}
{{- if and (eq $a 169) (eq $b 254) (eq (div $c $s) (div 169 $s)) }}true{{ end -}}
{{- else -}}
{{- $s := index $pow (sub 32 $p) -}}
{{- if and (eq $a 169) (eq $b 254) (eq $c 169) (eq (div $d $s) (div 254 $s)) }}true{{ end -}}
{{- end -}}
{{- end -}}
{{- end }}


{{/*
The ConfigMap holding the certificate authority the gateway trusts for the OIDC provider,
"name|key", or nothing: the chart's own (oidc.caCert) or one that exists (oidc.caConfigMap).
*/}}
{{- define "tabularia.oidcCa" -}}
{{- if .Values.oidc.enabled -}}
{{- $own := .Values.oidc.caCert -}}
{{- $existing := (.Values.oidc.caConfigMap | default dict) -}}
{{- if and $own $existing.name -}}
{{- fail "oidc.caCert and oidc.caConfigMap.name: set one of them, not both" -}}
{{- else if $own -}}
{{ include "tabularia.fullname" . }}-oidc-ca|ca.crt
{{- else if $existing.name -}}
{{ $existing.name }}|{{ $existing.key | default "ca.crt" }}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
A bound of a workers' autoscaler (an HPA's minReplicas or maxReplicas, a ScaledObject's
minReplicaCount or maxReplicaCount). With adminScaling, Administration → Workers sets them
on the cluster, as a field manager of its own: Helm 4 applies server side and stopped the
whole upgrade on such a field holding another value (a conflict). The live value is
rendered instead while another manager holds the field: the same value is no conflict,
and it is the one the panel would put back anyway.
Args (a dict): root, apiVersion, kind, name, field, value (the chart's)
*/}}
{{- define "tabularia.panelBound" -}}
{{- $v := .value -}}
{{- if .root.Values.adminScaling.enabled -}}
{{- $live := lookup .apiVersion .kind .root.Release.Namespace .name -}}
{{- if $live -}}
{{- range ($live.metadata.managedFields | default list) -}}
{{- if and (ne .manager "helm") (hasKey (dig "fieldsV1" "f:spec" (dict) .) (printf "f:%s" $.field)) -}}
{{- $v = index $live.spec $.field -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $v -}}
{{- end -}}
