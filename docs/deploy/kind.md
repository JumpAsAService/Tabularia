# A company-like cluster on one computer (kind)

This guide builds, on one computer, a Kubernetes cluster that behaves like a company's,
and installs Tabularia on it with the same Helm chart and the same settings you would
use there:
- three nodes, so pods are spread and images are pulled by more than one node;
- network policies and Pod Security that are enforced, not just written down;
- a private image registry with TLS, signed by a certificate authority of your own (a
  "company CA"), and a password;
- nodes that trust that authority, as a platform team sets them up;
- PostgreSQL, Valkey and object storage (MinIO) inside the cluster, in a namespace of
  their own;
- Tabularia's images copied into the private registry, and Python and R scripts built
  and run through it;
- images of your own run as steps of a flow (the Container node), from that registry.

It is meant for people who know a terminal and want to try Tabularia as it will run in
production, or test a change of the chart before a customer does. To simply try
Tabularia, [the minikube guide](minikube.md) is shorter and needs no Kubernetes knowledge.

It was written by running every command below, in this order, with kind 0.33
(Kubernetes 1.37, containerd 2.3).

## What you need

- **A computer with 16 GB of memory at least** (32 GB to build R environments
  comfortably), 8 cores and 60 GB of free disk.
- **Linux**, or **macOS** with Docker Desktop, or **Windows** inside WSL 2 (open an
  Ubuntu terminal and work there).
- [Docker](https://docs.docker.com/engine/install/), [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
  0.27 or later, [kubectl](https://kubernetes.io/docs/tasks/tools/),
  [Helm](https://helm.sh/docs/intro/install/), `openssl` and `git`.

Run every command in **the same terminal**: the steps keep values in shell variables
(`$WORK`, `$VERSION`, the passwords). If you open a new terminal, run
`source "$WORK/env"` (step 1 explains it) to get them back.

## 1. Get Tabularia and a working folder

```bash
git clone --branch v1.2.1 https://github.com/JumpAsAService/Tabularia.git
cd Tabularia
export VERSION=1.2.1 WORK="$HOME/tabularia-kind"
mkdir -p "$WORK"
```

Every later command runs from the `Tabularia` folder. `$WORK` holds what the cluster
needs outside of it: certificates, passwords, the settings. Choose now the first
administrator of Tabularia:

```bash
export ADMIN_EMAIL='you@example.com' ADMIN_PASSWORD='choose-a-long-password'
```

and keep every value in a file only you can read, so a new terminal finds them:

```bash
umask 077
printf 'export VERSION=%q WORK=%q ADMIN_EMAIL=%q ADMIN_PASSWORD=%q\n' \
  "$VERSION" "$WORK" "$ADMIN_EMAIL" "$ADMIN_PASSWORD" > "$WORK/env"
```

## 2. A certificate authority and the registry's certificate

The "company CA" signs the registry's certificate. In a company it already exists; here
you create one:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=Example Internal CA" \
  -keyout "$WORK/ca.key" -out "$WORK/ca.crt"
openssl req -newkey rsa:2048 -nodes -subj "/CN=tabularia-registry" \
  -keyout "$WORK/registry.key" -out "$WORK/registry.csr"
printf 'subjectAltName=DNS:tabularia-registry\n' > "$WORK/san.ext"
openssl x509 -req -in "$WORK/registry.csr" -CA "$WORK/ca.crt" -CAkey "$WORK/ca.key" \
  -CAcreateserial -days 365 -extfile "$WORK/san.ext" -out "$WORK/registry.crt"
openssl verify -CAfile "$WORK/ca.crt" "$WORK/registry.crt"
```

The last command answers `registry.crt: OK`.

## 3. Nodes that trust the authority

The nodes' container runtime (containerd) downloads the images, so it must trust the
registry's certificate. A platform team installs the authority on its nodes; kind lets
you give the nodes a folder of containerd's settings:

```bash
mkdir -p "$WORK/certs.d/tabularia-registry:5000"
cp "$WORK/ca.crt" "$WORK/certs.d/tabularia-registry:5000/ca.crt"
cat > "$WORK/certs.d/tabularia-registry:5000/hosts.toml" <<'EOF'
server = "https://tabularia-registry:5000"

[host."https://tabularia-registry:5000"]
  capabilities = ["pull", "resolve", "push"]
  ca = "/etc/containerd/certs.d/tabularia-registry:5000/ca.crt"
EOF
```

## 4. The cluster

One control-plane node and two workers. The control plane also receives the browser's
traffic: port 8080 of your computer goes to it.

```bash
cat > "$WORK/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: tabularia
nodes:
  - role: control-plane
    extraPortMappings:
      - { containerPort: 80, hostPort: 8080, listenAddress: "127.0.0.1" }
    extraMounts: [{ hostPath: "$WORK/certs.d", containerPath: /etc/containerd/certs.d }]
  - role: worker
    extraMounts: [{ hostPath: "$WORK/certs.d", containerPath: /etc/containerd/certs.d }]
  - role: worker
    extraMounts: [{ hostPath: "$WORK/certs.d", containerPath: /etc/containerd/certs.d }]
containerdConfigPatches:
  - |-
    [plugins."io.containerd.cri.v1.images".registry]
      config_path = "/etc/containerd/certs.d"
EOF
kind create cluster --config "$WORK/kind.yaml" --wait 3m
kubectl get nodes
```

Three nodes, `Ready`. kind applies network policies with its own network plugin, and
Kubernetes applies Pod Security: nothing more to install for them.

The browser reaches Tabularia through an ingress controller on the control plane:

```bash
kubectl label node tabularia-control-plane ingress-ready=true
kubectl apply -f https://kind.sigs.k8s.io/examples/ingress/deploy-ingress-nginx.yaml
kubectl wait --namespace ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=5m
```

## 5. The private registry

A registry with TLS and a user, on the same Docker network as the nodes, so they reach
it as `tabularia-registry:5000`. Your computer reaches it at `127.0.0.1:5443`.

```bash
export REGISTRY_PASSWORD="$(openssl rand -hex 16)"
echo "export REGISTRY_PASSWORD=$REGISTRY_PASSWORD" >> "$WORK/env"
mkdir -p "$WORK/auth"
docker run --rm --entrypoint htpasswd httpd:2.4 -Bbn tabularia "$REGISTRY_PASSWORD" > "$WORK/auth/htpasswd"
docker run -d --name tabularia-registry --network kind --restart unless-stopped -p 127.0.0.1:5443:5000 \
  -v "$WORK/registry.crt:/certs/registry.crt:ro" -v "$WORK/registry.key:/certs/registry.key:ro" \
  -v "$WORK/auth:/auth:ro" \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/registry.crt -e REGISTRY_HTTP_TLS_KEY=/certs/registry.key \
  -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=tabularia \
  -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
  registry:2.8.3
```

**Check:** without the password the registry refuses, with it it answers (the first
command waits a few seconds for the registry to start).

```bash
curl -s -o /dev/null -w '%{http_code}\n' --retry 10 --retry-all-errors --retry-delay 1 --cacert "$WORK/ca.crt" \
  --resolve tabularia-registry:5443:127.0.0.1 https://tabularia-registry:5443/v2/
curl -s -o /dev/null -w '%{http_code}\n' --cacert "$WORK/ca.crt" -u "tabularia:$REGISTRY_PASSWORD" \
  --resolve tabularia-registry:5443:127.0.0.1 https://tabularia-registry:5443/v2/
```

`401`, then `200`.

## 6. Tabularia's images in the private registry

A company copies the images it runs into its own registry. Do the same with the
published ones. The login goes in a Docker settings folder of its own, not in your
`~/.docker`:

```bash
export DOCKER_CONFIG="$WORK/docker"
echo "$REGISTRY_PASSWORD" | docker login 127.0.0.1:5443 -u tabularia --password-stdin
for c in backend gateway frontend; do
  docker pull "jumpasaservice/tabularia-${c}:${VERSION}"
  docker tag "jumpasaservice/tabularia-${c}:${VERSION}" "127.0.0.1:5443/tabularia/tabularia-${c}:${VERSION}"
  docker push "127.0.0.1:5443/tabularia/tabularia-${c}:${VERSION}"
done
unset DOCKER_CONFIG
```

Docker trusts registries on `127.0.0.1` without checking their certificate, so your
computer needs no change. The nodes download from `tabularia-registry:5000` and check
it, with the authority of step 3.

> **The code of a branch instead of a release** (to try a change before it is released):
> clone without `--branch`, take the version from the chart, and build the three images
> from the clone instead of pulling them. Then go on with step 7.
>
> ```bash
> export VERSION="$(sed -n 's/^appVersion: "\([^"]*\)".*/\1/p' infrastructure/helm/tabularia/Chart.yaml)"
> echo "export VERSION=$VERSION" >> "$WORK/env"
> export DOCKER_CONFIG="$WORK/docker"
> echo "$REGISTRY_PASSWORD" | docker login 127.0.0.1:5443 -u tabularia --password-stdin
> docker build -t "127.0.0.1:5443/tabularia/tabularia-backend:${VERSION}" backend/
> docker build -t "127.0.0.1:5443/tabularia/tabularia-gateway:${VERSION}" gateway/
> docker build -f frontend/Dockerfile.prod -t "127.0.0.1:5443/tabularia/tabularia-frontend:${VERSION}" frontend/
> for c in backend gateway frontend; do docker push "127.0.0.1:5443/tabularia/tabularia-${c}:${VERSION}"; done
> unset DOCKER_CONFIG
> ```
>
> The builds take a while the first time (the backend image is about 800 MB).

## 7. PostgreSQL, Valkey and MinIO

In the namespace `tabularia-deps`, with passwords made now:

```bash
export DB_PASSWORD="$(openssl rand -hex 32)" MINIO_PASSWORD="$(openssl rand -hex 24)" SCRIPTS_KEY="$(openssl rand -hex 24)"
printf 'export DB_PASSWORD=%s MINIO_PASSWORD=%s SCRIPTS_KEY=%s\n' "$DB_PASSWORD" "$MINIO_PASSWORD" "$SCRIPTS_KEY" >> "$WORK/env"
kubectl create namespace tabularia-deps
kubectl create secret generic postgres --namespace tabularia-deps --from-literal=password="$DB_PASSWORD"
kubectl create secret generic minio --namespace tabularia-deps \
  --from-literal=root-password="$MINIO_PASSWORD" --from-literal=scripts-key="$SCRIPTS_KEY"
kubectl apply -f infrastructure/minikube/dependencies.yaml -f infrastructure/kind/storage.yaml
kubectl wait --namespace tabularia-deps --for=condition=complete job/minio-setup --timeout=5m
```

The Job creates two buckets: `tabularia` for Tabularia's data, and `tabularia-scripts`
with a key of its own (`scripts`) for the Python and R scripts.

## 8. Tabularia's settings

The values a company would write: images from its registry, the dependencies in their
namespace, network policies on, scripting with the company's authority, the Container
node on.

```bash
cat > "$WORK/values.yaml" <<EOF
image:
  registry: "tabularia-registry:5000/tabularia"
  pullSecrets: [{ name: registry }]
externalServices:
  postgres: { host: postgres.tabularia-deps.svc.cluster.local }
  redis: { host: valkey.tabularia-deps.svc.cluster.local }
  storage: { endpoint: "http://minio.tabularia-deps.svc.cluster.local:9000", bucket: tabularia, region: us-east-1 }
secrets:
  fernetKey: "$(openssl rand -base64 32 | tr '+/' '-_')"
  jwtSecret: "$(openssl rand -hex 32)"
  dbPassword: "$DB_PASSWORD"
  storageAccessKey: root
  storageSecretKey: "$MINIO_PASSWORD"
  adminPassword: "$ADMIN_PASSWORD"
auth:
  adminEmail: "$ADMIN_EMAIL"
ingress:
  hosts:
    - host: ""
      paths: [{ path: /, pathType: Prefix, service: frontend }]
networkPolicy:
  enabled: true
  egressCIDRs: ["0.0.0.0/0"]
scripts:
  enabled: true
  registry:
    repository: "tabularia-registry:5000/tabularia-envs"
    pushSecret: registry
    pullSecret: registry
    caCert: |
$(sed 's/^/      /' "$WORK/ca.crt")
containers:
  enabled: true
EOF
```

What some of them do:
- `networkPolicy.egressCIDRs: ["0.0.0.0/0"]` turns the policy on: the workers reach the
  internet and your databases, but never the cloud metadata address. A company lists
  its own ranges instead. The namespace `tabularia-deps` is allowed from the
  dependencies' host names.
- `scripts.registry.caCert` is the authority of step 2: the image builds verify the
  registry with it.
- `containers.enabled` creates the namespaces where the Container nodes run,
  `tabularia-containers` and `tabularia-containers-dev`, at the `baseline` Pod Security
  level.

## 9. Install

Tabularia's namespace, with the registry's password for the image downloads, then the
chart:

```bash
kubectl create namespace tabularia
kubectl create secret docker-registry registry --namespace tabularia \
  --docker-server=tabularia-registry:5000 --docker-username=tabularia --docker-password="$REGISTRY_PASSWORD"
helm install tabularia infrastructure/helm/tabularia --namespace tabularia --values "$WORK/values.yaml"
```

The chart creates the three namespaces of the scripts. They need the registry's password
too: the builds push with it, the scripts pull with it.

```bash
for ns in tabularia-scripts tabularia-scripts-dev tabularia-script-builds; do
  kubectl create secret docker-registry registry --namespace "$ns" \
    --docker-server=tabularia-registry:5000 --docker-username=tabularia --docker-password="$REGISTRY_PASSWORD"
done
kubectl get pods --namespace tabularia --watch
```

When every pod is `1/1 Running`, press Ctrl+C and open **http://localhost:8080**. Sign in
with `$ADMIN_EMAIL` and `$ADMIN_PASSWORD`.

## 10. Python and R scripts

In **Administration → Scripting environments**, the scripts bucket:

| Field | Value |
|---|---|
| Endpoint | `http://minio.tabularia-deps.svc.cluster.local:9000` |
| Region | `us-east-1` |
| Bucket | `tabularia-scripts` |
| Access key | `scripts` |
| Secret key | the value of `$SCRIPTS_KEY` (`echo "$SCRIPTS_KEY"`) |

**Test** answers that it wrote, read and deleted a probe object, and that the key cannot
list the bucket. Then create an environment, for example Python 3.12. Its image is built
in the cluster, pushed to the private registry with the company's authority, checked,
and marked **ready** after a few minutes. Script nodes can use it: see
[Python and R in flows](../scripting.md).

## 11. Images of your own

The Container node runs an image of yours as a step of a flow. Put one in the registry,
for example a small one to begin with:

```bash
export DOCKER_CONFIG="$WORK/docker"
docker pull busybox:1.36.1
docker tag busybox:1.36.1 127.0.0.1:5443/team/busybox:1.36
docker push 127.0.0.1:5443/team/busybox:1.36
unset DOCKER_CONFIG
```

In **Administration → Container registries**, **New registry**:

| Field | Value |
|---|---|
| Name | `Company registry` |
| Address | `tabularia-registry:5000` |
| Protocol | HTTPS |
| Certificate | **Company authority**, and paste the content of `$WORK/ca.crt` (`cat "$WORK/ca.crt"`) |
| User | `tabularia` |
| Password or token | the value of `$REGISTRY_PASSWORD` (`echo "$REGISTRY_PASSWORD"`) |
| Allowed repositories | `team/*` |

Then, on the registry's page:
- **Test** answers that the registry lets Tabularia in as `tabularia`;
- **Try in the cluster**, with `team/busybox` and `1.36`, answers that a node of the
  cluster downloaded it: the nodes trust the authority (step 3) and Tabularia gave them
  the password, in a pull Secret of its own in `tabularia-containers-dev`.

In a flow, the palette now offers **Container**: choose the registry, the image
`team/busybox`, the tag `1.36`, and as command `sh` and `-c` (one per line) with the
argument `echo hello from $HOSTNAME`. **Run** in its panel runs it, and its log shows the
greeting. [The Container node](../container-node.md) explains its input and output,
its variables and the rest.

Tabularia needs no Secret of yours in the containers' namespaces: it writes the
registry's own from the credentials above, right before each run.

## What this cluster shows

| Here | In a company's cluster |
|---|---|
| `certs.d` mounted on the kind nodes | the authority installed on the nodes by the platform team (node image, DaemonSet) |
| `registry:2.8.3` with a password | Harbor, GitLab, the cloud provider's registry |
| `0.0.0.0/0` in `egressCIDRs` | the ranges of the company's databases and services |
| PostgreSQL, Valkey and MinIO in `tabularia-deps` | managed services, or the company's own charts |
| the ingress on `localhost:8080` | the company's ingress, with TLS and a host name |

When something is refused, Tabularia says which side: a build that cannot push names
`scripts.registry.caCert`; a script whose image a node cannot download fails in about
half a minute saying whether the credentials, the certificate or the image is the
problem.

## Remove it

```bash
kind delete cluster --name tabularia
docker rm -f tabularia-registry
rm -rf "$WORK"
```

kind clusters are made to be thrown away: if one does not come back after the computer
restarts, remove it and run the guide again.

## When something goes wrong

- **`kind create cluster` fails on the port**: something already listens on 8080. Change
  `hostPort` in `$WORK/kind.yaml` and open that port instead.
- **A Container node, or Try in the cluster, says the nodes do not trust the
  registry's certificate**: the authority is missing from `certs.d` for that address
  (step 3); a registry under another name needs a folder of its own there.
- **Pods in `ErrImagePull`**: `kubectl describe pod <name> --namespace <namespace>` says
  why. *x509* means the nodes do not trust the registry (step 3 and the `extraMounts`
  of step 4); *401* or *unauthorized*, a missing or wrong `registry` Secret in that
  namespace.
- **A build fails with "not pushed"**: the message says the certificate
  (`scripts.registry.caCert`) or the credentials (the `registry` Secret of
  `tabularia-script-builds`).
- **Workers time out on Valkey or MinIO**: a network policy blocks them. The chart allows
  the namespaces of the hosts in `externalServices`; check that those hosts end in
  `.svc.cluster.local`.
- **A variable is empty in a new terminal**: run `source "$WORK/env"` (and set `WORK`
  first if it is empty too).
