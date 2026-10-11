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
{{- range list "STORAGE__ACCESS_KEY" "STORAGE__SECRET_KEY" "REDIS__PASSWORD" "CLICKHOUSE_EXTERNAL__PASSWORD" "CLICKHOUSE_EXTERNAL__AI_PASSWORD" "BIGQUERY__CREDENTIALS_B64" }}
- name: {{ . }}
  value: ""
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
