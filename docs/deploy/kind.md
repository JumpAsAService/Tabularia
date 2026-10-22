# A company-like cluster on one computer (kind)

This guide builds, on one computer, a Kubernetes cluster that behaves like a company's,
and installs Tabularia on it with the same Helm chart and the same settings you would
use there:
- three nodes, so pods are spread and images are pulled by more than one node;
- network policies and Pod Security that are enforced, not just written down, with
  Calico as the network plugin (or Cilium);
- a private image registry with TLS, signed by a certificate authority of your own (a
  "company CA"), and a password;
- nodes that trust that authority, as a platform team sets them up;
- the scripts and the Container nodes kept away from Tabularia's own services by
  network policies;
- PostgreSQL, Valkey and object storage inside the cluster, in a namespace of their own;
  or, as most companies do, the object storage of your cloud (step 7) and an external
  ClickHouse for the flows (step 12);
- Tabularia's images copied into the private registry, and Python and R scripts built
  and run through it;
- images of your own run as steps of a flow (the Container node), from that registry.

It is meant for people who know a terminal and want to try Tabularia as it will run in
production, or test a change of the chart before a customer does. To simply try
Tabularia, [the minikube guide](minikube.md) is shorter and needs no Kubernetes knowledge.

It was written by running every command below, in this order, with kind 0.33
(Kubernetes 1.37, containerd 2.3) and Calico 3.30.

## What you need

- **A computer with 16 GB of memory at least** (32 GB to build R environments
  comfortably), 8 cores and 60 GB of free disk.
- **Linux**, or **macOS** with Docker Desktop, or **Windows** inside WSL 2 (an Ubuntu
  that runs on Windows: [On Windows](#on-windows) below sets it up).
- [Docker](https://docs.docker.com/engine/install/), [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
  0.27 or later, [kubectl](https://kubernetes.io/docs/tasks/tools/),
  [Helm](https://helm.sh/docs/intro/install/), `openssl` and `git`.

Run every command in **the same terminal**: the steps keep values in shell variables
(`$WORK`, `$VERSION`, the passwords). If you open a new terminal, run
`source "$WORK/env"` (step 1 explains it) to get them back.

### On Windows

The guide runs in an Ubuntu inside Windows (WSL 2), where every command works as on
Linux. Set it up once:

1. **Install WSL 2 with Ubuntu.** In a PowerShell opened as administrator:
   `wsl --install -d Ubuntu`, then restart the computer. Ubuntu asks for a user name and
   a password the first time it opens.
2. **Give it memory and processors.** WSL takes half of the computer's memory by
   default. In PowerShell, `notepad "$env:USERPROFILE\.wslconfig"`, write

   ```ini
   [wsl2]
   memory=24GB
   processors=8
   ```

   (16 GB at least; on a 32 GB computer, 24 leaves Windows enough), save, run
   `wsl --shutdown`, and open Ubuntu again.
3. **Install Docker inside Ubuntu**: Docker Engine, following
   [Docker's guide for Ubuntu](https://docs.docker.com/engine/install/ubuntu/), then
   `sudo usermod -aG docker $USER` and open Ubuntu again. Docker Engine inside Ubuntu
   behaves as on Linux, ports and mounted folders included, which is what this guide
   was run on. Docker Desktop with its WSL integration can work too, but its ports
   pass through one more machine.
4. **Install the tools inside Ubuntu**, in their Linux versions:
   `sudo apt install -y openssl git`, then kind, kubectl and Helm from the links above
   (the Linux instructions).
5. **Work in Ubuntu's home** (`cd ~`), not in `/mnt/c/…`: the Windows disk is much
   slower from Ubuntu, and it does not keep the permissions that protect the files of
   passwords this guide writes.

Run the guide in the Ubuntu terminal, unchanged. The browser of Windows opens
Tabularia at **http://localhost:8080**: WSL 2 hands the ports it listens on over to
Windows.

## 1. Get Tabularia and a working folder

```bash
git clone --branch v1.2.2 https://github.com/JumpAsAService/Tabularia.git
cd Tabularia
export VERSION=1.2.2 WORK="$HOME/tabularia-kind"
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
traffic: port 8080 of your computer goes to it. kind's own network plugin enforces only
the rules on what comes into a pod, so the cluster starts without it, and Calico, a
network plugin many companies run, takes its place: it enforces the rules on what goes
out too.

```bash
cat > "$WORK/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: tabularia
networking:
  disableDefaultCNI: true
  podSubnet: 10.244.0.0/16
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
kind create cluster --config "$WORK/kind.yaml"
```

The nodes are `NotReady` until a network plugin runs. Calico, with the pods' addresses
of the cluster above:

```bash
curl -fsSL https://raw.githubusercontent.com/projectcalico/calico/v3.30.3/manifests/calico.yaml \
  | sed -e 's|# - name: CALICO_IPV4POOL_CIDR|- name: CALICO_IPV4POOL_CIDR|' \
        -e 's|#   value: "192.168.0.0/16"|  value: "10.244.0.0/16"|' > "$WORK/calico.yaml"
kubectl apply -f "$WORK/calico.yaml"
kubectl rollout status daemonset calico-node --namespace kube-system --timeout=10m
kubectl wait --for=condition=Ready nodes --all --timeout=5m
kubectl get nodes
```

Three nodes, `Ready`. Calico enforces the network policies, Kubernetes applies Pod
Security: nothing more to install for them.

> **Cilium instead of Calico**, if your company runs it. Install the
> [Cilium CLI](https://docs.cilium.io/en/stable/gettingstarted/k8s-install-default/#install-the-cilium-cli),
> then, in place of the Calico commands above:
>
> ```bash
> cilium install --version 1.20.3
> kubectl rollout status daemonset cilium --namespace kube-system --timeout=10m
> kubectl wait --for=condition=Ready nodes --all --timeout=5m
> ```
>
> Everything else in this guide stays the same: Tabularia's network policies are
> standard Kubernetes ones, and Cilium enforces them as Calico does.

The browser reaches Tabularia through an ingress controller on the control plane:

```bash
kubectl label node tabularia-control-plane ingress-ready=true
kubectl apply -f https://kind.sigs.k8s.io/examples/ingress/deploy-ingress-nginx.yaml
kubectl patch deployment ingress-nginx-controller --namespace ingress-nginx \
  -p '{"spec":{"template":{"spec":{"nodeSelector":{"ingress-ready":"true","kubernetes.io/os":"linux"}}}}}'
kubectl rollout status deployment ingress-nginx-controller --namespace ingress-nginx --timeout=5m
```

The `patch` keeps the controller on the control plane, the only node that receives port
8080: kind's manifest lets it start on any of the three nodes. **Check:**
`curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/` answers `404` (nothing
is installed behind it yet), not `000`.

CPU and memory of the pods, for `kubectl top` and for Tabularia's **Administration →
Cluster**, come from metrics-server. kind's nodes have self-signed certificates, which
metrics-server is told to accept:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
kubectl patch deployment metrics-server --namespace kube-system --type json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
kubectl rollout status deployment metrics-server --namespace kube-system --timeout=5m
```

**Check:** after a minute, `kubectl top nodes` lists the three nodes with their CPU and
memory.

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

## 7. PostgreSQL, Valkey and the object storage

PostgreSQL and Valkey run in the namespace `tabularia-deps`, with a password made now:

```bash
export DB_PASSWORD="$(openssl rand -hex 32)"
printf 'export DB_PASSWORD=%s\n' "$DB_PASSWORD" >> "$WORK/env"
kubectl create namespace tabularia-deps
kubectl create secret generic postgres --namespace tabularia-deps --from-literal=password="$DB_PASSWORD"
kubectl apply -f infrastructure/minikube/dependencies.yaml
```

The file also holds a network policy: only Tabularia's namespace (created in step 9)
reaches the database and the queue. The scripts and the Container nodes may reach this
namespace, for the scripts bucket when it is MinIO, so without that policy a script
could write into the workers' queue.

Tabularia keeps its datasets, uploads and results in an S3-compatible bucket, and the
Python and R scripts exchange their data through a second one. Choose **one** of the
two ways below: MinIO inside the cluster, to have everything on this computer, or the
object storage of your cloud, as a company runs it.

### Either: MinIO inside the cluster

```bash
export MINIO_PASSWORD="$(openssl rand -hex 24)" SCRIPTS_KEY="$(openssl rand -hex 24)"
kubectl create secret generic minio --namespace tabularia-deps \
  --from-literal=root-password="$MINIO_PASSWORD" --from-literal=scripts-key="$SCRIPTS_KEY"
kubectl apply -f infrastructure/kind/storage.yaml
kubectl wait --namespace tabularia-deps --for=condition=complete job/minio-setup --timeout=5m
export STORAGE_ENDPOINT="http://minio.tabularia-deps.svc.cluster.local:9000" STORAGE_REGION=us-east-1 \
  STORAGE_BUCKET=tabularia STORAGE_ACCESS_KEY=root STORAGE_SECRET_KEY="$MINIO_PASSWORD" \
  SCRIPTS_ENDPOINT="http://minio.tabularia-deps.svc.cluster.local:9000" SCRIPTS_REGION=us-east-1 \
  SCRIPTS_BUCKET=tabularia-scripts SCRIPTS_ACCESS_KEY=scripts SCRIPTS_SECRET_KEY="$SCRIPTS_KEY"
```

The Job creates the two buckets: `tabularia` for Tabularia's data, and
`tabularia-scripts` with a key of its own (`scripts`) for the scripts.

### Or: the object storage of your cloud

In your cloud provider's console:

1. **Create two private buckets** in the same region, for example
   `tabularia-yourcompany` for Tabularia's data and `tabularia-yourcompany-scripts` for
   the scripts. On the scripts bucket, add a rule that deletes its objects after one day.
2. **Create two access keys**: one that reads, writes, lists and deletes objects in the
   data bucket; one that reads, writes and deletes objects in the scripts bucket, without
   listing it. [The scripts bucket](../scripting.md#the-scripts-bucket) shows the policies
   for each provider.
3. **Note the endpoint and the region** of the buckets:

| Provider | Endpoint | Region, for example |
|---|---|---|
| Scaleway | `https://s3.fr-par.scw.cloud` (or `nl-ams`, `pl-waw`) | `fr-par` |
| AWS | `https://s3.eu-west-1.amazonaws.com` (your region) | `eu-west-1` |
| Other S3-compatible | the S3 address in your provider's documentation | its region |

> **Scaleway:** an API key works on its **preferred project** for Object Storage. When
> you create the key (IAM → API keys), choose the project that holds the buckets.

Then give the values to the terminal (replace every `<…>`; the quotes stay):

```bash
export STORAGE_ENDPOINT="<https://s3.fr-par.scw.cloud>" STORAGE_REGION="<fr-par>" \
  STORAGE_BUCKET="<tabularia-yourcompany>" \
  STORAGE_ACCESS_KEY="<access key of the data bucket>" STORAGE_SECRET_KEY="<its secret key>" \
  SCRIPTS_ENDPOINT="<https://s3.fr-par.scw.cloud>" SCRIPTS_REGION="<fr-par>" \
  SCRIPTS_BUCKET="<tabularia-yourcompany-scripts>" \
  SCRIPTS_ACCESS_KEY="<access key of the scripts bucket>" SCRIPTS_SECRET_KEY="<its secret key>"
```

Nothing about the bucket is in the cluster: the workers, the scripts and the Container
nodes reach it over the internet, which the network policies of step 8 allow.

### Both ways: keep the values

```bash
printf 'export STORAGE_ENDPOINT=%q STORAGE_REGION=%q STORAGE_BUCKET=%q STORAGE_ACCESS_KEY=%q STORAGE_SECRET_KEY=%q\n' \
  "$STORAGE_ENDPOINT" "$STORAGE_REGION" "$STORAGE_BUCKET" "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >> "$WORK/env"
printf 'export SCRIPTS_ENDPOINT=%q SCRIPTS_REGION=%q SCRIPTS_BUCKET=%q SCRIPTS_ACCESS_KEY=%q SCRIPTS_SECRET_KEY=%q\n' \
  "$SCRIPTS_ENDPOINT" "$SCRIPTS_REGION" "$SCRIPTS_BUCKET" "$SCRIPTS_ACCESS_KEY" "$SCRIPTS_SECRET_KEY" >> "$WORK/env"
```

## 8. Tabularia's settings

The values a company would write: images from its registry, the dependencies in their
namespace, network policies on, scripting with the company's authority, the Container
node on.

The image builds push to the registry, and on this computer the registry has a private
address on kind's Docker network. The scripts' network policy closes private addresses
unless they are listed, so read the registry's:

```bash
export REGISTRY_IP="$(docker exec tabularia-control-plane getent ahostsv4 tabularia-registry | awk 'NR==1 {print $1}')"
echo "$REGISTRY_IP"
printf 'export REGISTRY_IP=%s\n' "$REGISTRY_IP" >> "$WORK/env"
```

It prints one address, such as `172.18.0.5`. Then the settings:

```bash
cat > "$WORK/values.yaml" <<EOF
image:
  registry: "tabularia-registry:5000/tabularia"
  pullSecrets: [{ name: registry }]
externalServices:
  postgres: { host: postgres.tabularia-deps.svc.cluster.local }
  redis: { host: valkey.tabularia-deps.svc.cluster.local }
  storage: { endpoint: "$STORAGE_ENDPOINT", bucket: "$STORAGE_BUCKET", region: "$STORAGE_REGION" }
secrets:
  fernetKey: "$(openssl rand -base64 32 | tr '+/' '-_')"
  jwtSecret: "$(openssl rand -hex 32)"
  dbPassword: "$DB_PASSWORD"
  storageAccessKey: "$STORAGE_ACCESS_KEY"
  storageSecretKey: "$STORAGE_SECRET_KEY"
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
  userCodePrivateCIDRs: ["$REGISTRY_IP/32"]
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
  internet (a cloud bucket, an external ClickHouse) and your databases, but never the
  cloud metadata address. A company lists its own ranges instead. The namespace
  `tabularia-deps` is allowed from the dependencies' host names.
- `networkPolicy.userCodePrivateCIDRs` is for the scripts, their image builds and the
  Container nodes. Their namespaces get a policy of their own: they reach the internet
  and the other namespaces of the cluster, never Tabularia's namespace, and no private
  address outside the cluster unless it is listed here. List the registry's address
  only, not kind's whole network: that network holds the nodes too.
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

The chart creates the three namespaces of the scripts (and the two of the Container
nodes, step 11). The scripts' ones need the registry's password
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

**Administration → Cluster** shows, refreshed every three seconds, the pods of Tabularia
and, from step 10, the Jobs of the scripts and containers while they run, each linked to
the flow or script that started it: their state, restarts, CPU and memory against their
limits, and the cluster's warning events.

## 10. Python and R scripts

In **Administration → Scripting environments**, the scripts bucket of step 7:

| Field | Value (`echo` it in the terminal) |
|---|---|
| Endpoint | `$SCRIPTS_ENDPOINT` |
| Region | `$SCRIPTS_REGION` |
| Bucket | `$SCRIPTS_BUCKET` |
| Access key | `$SCRIPTS_ACCESS_KEY` |
| Secret key | `$SCRIPTS_SECRET_KEY` |

**Test** answers that it wrote, read and deleted a probe object, and that the key cannot
list the bucket. Then create an environment, for example Python 3.12. Its image is built
in the cluster, pushed to the private registry with the company's authority, checked,
and marked **Ready** after a few minutes. Script nodes can use it: see
[Python and R in flows](../scripting.md). Scripts can also be kept on their own, next to
flows and views: **Explore → New → Script**.

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

## 12. Optional: flows on an external ClickHouse

Tabularia can send the work of a flow to a ClickHouse service instead of doing it in its
workers. ClickHouse then reads and writes the files of the bucket directly, so this
goes with **the object storage of your cloud** (step 7), with the ClickHouse service in
the same cloud and region. A ClickHouse service reachable over HTTPS (port 8443), a user
and its password are needed.

**In ClickHouse**, in the service's SQL console, as its administrator:

```sql
CREATE DATABASE IF NOT EXISTS tabularia;
CREATE NAMED COLLECTION tabularia_s3 AS
    access_key_id = '<access key of the data bucket>',
    secret_access_key = '<its secret key>';
```

The *named collection* keeps the bucket's keys inside ClickHouse, so they never travel
in the queries Tabularia sends. Tabularia is better given a user of its own than the
administrator; these grants are enough for it:

```sql
GRANT CREATE TEMPORARY TABLE, S3 ON *.* TO `<clickhouse-user>`;
GRANT NAMED COLLECTION ON tabularia_s3 TO `<clickhouse-user>`;
```

With those alone the Viewer reads the bucket's files at every query. To let it keep a
copy of the datasets over five million rows inside ClickHouse, which is faster, also
`GRANT CREATE TABLE, DROP TABLE, INSERT, SELECT ON tabularia.* TO <clickhouse-user>;`.

**In the terminal**, the service and the user (replace every `<…>`), then the settings
added to those of step 8:

```bash
export CLICKHOUSE_HOST="<your-service.clickhouse.example>" CLICKHOUSE_USER="<clickhouse-user>" \
  CLICKHOUSE_PASSWORD="<its password>"
printf 'export CLICKHOUSE_HOST=%q CLICKHOUSE_USER=%q CLICKHOUSE_PASSWORD=%q\n' \
  "$CLICKHOUSE_HOST" "$CLICKHOUSE_USER" "$CLICKHOUSE_PASSWORD" >> "$WORK/env"
cat > "$WORK/clickhouse.yaml" <<EOF
externalServices:
  clickhouse:
    enabled: true
    host: "$CLICKHOUSE_HOST"
    port: 8443
    secure: true
    database: tabularia
    username: "$CLICKHOUSE_USER"
    transport: s3
    s3Endpoint: "$STORAGE_ENDPOINT"
    s3NamedCollection: tabularia_s3
secrets:
  clickhousePassword: "$CLICKHOUSE_PASSWORD"
EOF
helm upgrade tabularia infrastructure/helm/tabularia --namespace tabularia \
  --values "$WORK/values.yaml" --values "$WORK/clickhouse.yaml"
```

After a minute, **Administration → Engines** lists ClickHouse. Choose it as your
**Preferred engine** in **Settings**, or as the engine of a flow, and open a datasource in
the **Viewer**: its rows come from ClickHouse. Every later `helm upgrade` takes both
files, in this order.

## What this cluster shows

| Here | In a company's cluster |
|---|---|
| metrics-server with `--kubelet-insecure-tls` | the metrics-server of the cluster, usually already there on a managed one |
| Calico (or Cilium) in place of kind's network plugin | the network plugin of the company's cluster: Calico, Cilium or the cloud provider's, with network policies on |
| `certs.d` mounted on the kind nodes | the authority installed on the nodes by the platform team (node image, DaemonSet) |
| `registry:2.8.3` with a password | Harbor, GitLab, the cloud provider's registry |
| `0.0.0.0/0` in `egressCIDRs` | the ranges of the company's databases and services |
| the registry's address in `userCodePrivateCIDRs` | the private ranges of the company's registry and services the scripts must reach; none when they are public |
| the policy in `tabularia-deps` | a managed database and queue on private addresses, which the scripts cannot reach unless listed in `userCodePrivateCIDRs` |
| PostgreSQL, Valkey and MinIO in `tabularia-deps` | managed services, or the company's own charts; the cloud bucket of step 7 is already what a company uses |
| the ingress on `localhost:8080` | the company's ingress, with TLS and a host name |

When something is refused, Tabularia says which side: a build that cannot push names
`scripts.registry.caCert`; a script whose image a node cannot download fails in about
half a minute saying whether the credentials, the certificate or the image is the
problem.

## Upgrade it

Build and push the new release's images as in step 6, then follow
[Upgrading Tabularia](upgrading.md): the commands of its minikube part work here too,
since PostgreSQL is in the same namespace `tabularia-deps`. MinIO's volume, like
PostgreSQL's, lives as long as the cluster.

## Remove it

```bash
kind delete cluster --name tabularia
docker rm -f tabularia-registry
rm -rf "$WORK"
```

kind clusters are made to be thrown away: if one does not come back after the computer
restarts, remove it and run the guide again.

## When something goes wrong

- **The nodes stay `NotReady`**: the network plugin is not running yet.
  `kubectl get pods --namespace kube-system` shows the `calico-node` (or `cilium`) pods
  and why they wait; an image still downloading takes a few minutes.
- **`kind create cluster` fails on the port**: something already listens on 8080. Change
  `hostPort` in `$WORK/kind.yaml` and open that port instead.
- **A Container node, or Try in the cluster, says the nodes do not trust the
  registry's certificate**: the authority is missing from `certs.d` for that address
  (step 3); a registry under another name needs a folder of its own there.
- **http://localhost:8080 does not answer** while every pod is `Running`: the ingress
  controller runs on a worker. `kubectl get pods --namespace ingress-nginx -o wide` shows
  its node; run the `patch` of step 4 again.
- **Pods in `ErrImagePull`**: `kubectl describe pod <name> --namespace <namespace>` says
  why. *x509* means the nodes do not trust the registry (step 3 and the `extraMounts`
  of step 4); *401* or *unauthorized*, a missing or wrong `registry` Secret in that
  namespace.
- **A build fails with "not pushed"**: the message says the certificate
  (`scripts.registry.caCert`) or the credentials (the `registry` Secret of
  `tabularia-script-builds`).
- **A build fails and the end of its log shows `dial tcp` to the registry's address
  with a timeout**: the scripts' network policy does not let the builds reach it. The
  registry's address can change when the computer restarts: run the `REGISTRY_IP`
  command of step 8 again, fix `userCodePrivateCIDRs` in `$WORK/values.yaml`, and run
  `helm upgrade` again.
- **Workers time out on Valkey or MinIO**: a network policy blocks them. The chart allows
  the namespaces of the hosts in `externalServices`; check that those hosts end in
  `.svc.cluster.local`.
- **Uploads or previews fail with a storage error** (*403*, *Access Denied*,
  *SignatureDoesNotMatch*, *NoSuchBucket*) with a cloud bucket: check the endpoint,
  region, bucket and both keys of step 7 (and, on Scaleway, the key's preferred
  project), fix `$WORK/values.yaml`, and run `helm upgrade` again.
- **Flows on ClickHouse fail reading the data** (*Access Denied*, *named collection does
  not exist*): check the named collection of step 12 and its `GRANT`, and that
  `s3Endpoint` is the bucket's address as the ClickHouse service sees it.
- **A variable is empty in a new terminal**: run `source "$WORK/env"` (and set `WORK`
  first if it is empty too).
