# Try Tabularia on your computer with minikube

This guide installs Tabularia on one computer, step by step. You do not need to know
Kubernetes: every command is given in full, and each step says what it does and how to
check that it worked. It works on Windows, macOS and Linux.

What you get at the end:
- Tabularia in your browser at `http://localhost:8080`, with you as its administrator;
- your data kept in a bucket of your cloud object storage (Scaleway, AWS or any
  S3-compatible service), not on your laptop;
- optionally, flows that run on your ClickHouse service, and Python and R scripts in
  flows.

Plan about an hour the first time, most of it waiting for downloads.

> **A trial, not a server.** Everything runs on your computer: when it sleeps or shuts
> down, Tabularia stops. To install Tabularia for a team, on a real Kubernetes cluster,
> see [Kubernetes and Helm](kubernetes.md). To try it on one computer the way a company
> runs it (several nodes, a private registry), see [the kind guide](kind.md).

## What you will set up

| Piece | What it is | Where it runs |
|---|---|---|
| **Docker** | Runs the programs below in isolated boxes (containers) | your computer |
| **minikube** | A small Kubernetes: it starts and watches the containers | your computer, inside Docker |
| **Tabularia** | The application, installed with **Helm** | inside minikube |
| **PostgreSQL** and **Valkey** | Tabularia's own database and job queue | inside minikube |
| **Your bucket** | Every dataset, upload and flow output | your cloud |
| ClickHouse *(optional)* | Runs flows on your ClickHouse service | your cloud |

## Before you start

**Your computer**
- 16 GB of memory (RAM), or 8 GB if you skip Python and R scripts;
- 4 processor cores;
- 40 GB of free disk space;
- Windows 10 or 11, macOS 12 or later, or a recent Linux;
- the right to install programs (only for step 1).

**Your cloud account**, with object storage. You will create a bucket and a key in step 4.

**A terminal**, the window where you type commands:
- **Windows**: open the Start menu, type `PowerShell`, open **Windows PowerShell**.
- **macOS**: open **Terminal** (Applications → Utilities).
- **Linux**: your usual terminal.

Copy each command exactly, one at a time, and press Enter. When a command differs
between systems, the guide shows one block per system. Text between `<` and `>`, such
as `<your-password>`, is a placeholder: replace all of it, `<` and `>` included, with
your own value.

## Step 1 — Install the tools

You need four programs: Docker, minikube, kubectl (the command that talks to
Kubernetes) and Helm (the command that installs Tabularia).

**Windows**

1. Install **Docker Desktop** from [docker.com](https://www.docker.com/products/docker-desktop/).
   During the installation keep **Use WSL 2** selected. Restart the computer when asked,
   then open Docker Desktop once and wait until it says it is running.
2. In PowerShell, install the other three:

   ```powershell
   winget install --exact --id Kubernetes.minikube
   winget install --exact --id Kubernetes.kubectl
   winget install --exact --id Helm.Helm
   ```

3. **Close PowerShell and open it again**, so it finds the new commands.

**macOS**

1. Install **Docker Desktop** from [docker.com](https://www.docker.com/products/docker-desktop/)
   and open it once.
2. With [Homebrew](https://brew.sh):

   ```bash
   brew install minikube kubectl helm
   ```

**Linux**

Install Docker Engine ([docs.docker.com/engine/install](https://docs.docker.com/engine/install/)),
then minikube ([minikube.sigs.k8s.io](https://minikube.sigs.k8s.io/docs/start/)),
kubectl ([kubernetes.io](https://kubernetes.io/docs/tasks/tools/)) and Helm
([helm.sh](https://helm.sh/docs/intro/install/)) following their pages.

> Docker Desktop is free for personal use, education and small businesses. Larger
> companies need a paid Docker subscription: check with your IT department.

**Check:** each of these prints a version number, not an error.

```bash
docker version
minikube version
kubectl version --client
helm version
```

## Step 2 — Start minikube

**Windows only: give Docker enough memory first.** Docker Desktop on Windows gets half of
the computer's memory by default, too little for 12 GB. In PowerShell:

```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

Notepad asks to create the file: answer **Yes**, write these two lines, save, close:

```ini
[wsl2]
memory=14GB
```

Then quit Docker Desktop (its icon near the clock → **Quit**), run `wsl --shutdown`, and
open Docker Desktop again. On a computer with 8 GB, skip this and use `--memory=6g` below.

**macOS only:** in Docker Desktop → **Settings** → **Resources**, set Memory to at least
14 GB (6 GB on an 8 GB computer), then **Apply & restart**.

**Start minikube** (all systems):

```bash
minikube start --driver=docker --cpus=4 --memory=12g --disk-size=40g
```

Without Python and R scripts, `--memory=6g` is enough. The first start downloads about
1 GB and takes a few minutes.

> Choose the memory now: minikube keeps the size it was created with. Changing it later
> means deleting minikube and starting again from this step.

Then turn on the component that brings the browser to Tabularia:

```bash
minikube addons enable ingress
```

**Check:** `kubectl get nodes` shows one line, `minikube`, with STATUS `Ready`.

## Step 3 — Download Tabularia

1. Open [the latest release](https://github.com/JumpAsAService/Tabularia/releases/latest).
2. Under **Assets**, click **Source code (zip)**.
3. Unzip it, for example in your Documents folder. You get a folder such as
   `Tabularia-1.2.2`, with a folder `infrastructure` inside.
4. Open your terminal **in that folder**:
   - **Windows**: open the folder in File Explorer, right-click an empty area,
     **Open in Terminal**. On Windows 10: hold Shift, right-click, **Open PowerShell
     window here**.
   - **macOS**: in Finder, right-click the folder → **New Terminal at Folder**.
   - **Linux**: `cd` into it.

Every command from here on runs in that folder.

**Check:** `helm show chart infrastructure/helm/tabularia` prints the chart's name,
`tabularia`, and its version.

## Step 4 — Prepare your cloud bucket

Tabularia keeps every dataset, upload and result in an S3-compatible bucket. In your
cloud provider's console:

1. **Create a bucket**, private (not public), in the region closest to you. Choose a
   name of your own, for example `tabularia-yourcompany`.
2. **Create an access key and its secret key** allowed to read, write, list and delete
   objects in that bucket. Copy both: the secret key is shown only once.
3. **Note the endpoint and the region** of the bucket:

| Provider | Endpoint | Region, for example |
|---|---|---|
| Scaleway | `https://s3.fr-par.scw.cloud` (or `nl-ams`, `pl-waw`) | `fr-par` |
| AWS | `https://s3.eu-west-1.amazonaws.com` (your region) | `eu-west-1` |
| Other S3-compatible | the S3 address in your provider's documentation | its region |

> **Scaleway:** an API key works on its **preferred project** for Object Storage. When
> you create the key (IAM → API keys), choose the project that holds the bucket.
> Otherwise the key is valid but Tabularia cannot see the bucket.

This bucket is your data. Costs are your provider's storage and traffic prices.

## Step 5 — Create your keys and passwords

Tabularia needs three secret values that you generate once, plus a password you choose.
Open a text file (or your password manager) to keep them; you will paste them in step 7.

| Name | What it does | How to make it |
|---|---|---|
| `fernetKey` | Encrypts the passwords of the connections people save | command A |
| `jwtSecret` | Signs the sign-in sessions | command B |
| `dbPassword` | Password of Tabularia's PostgreSQL database | command B, run again |
| `adminPassword` | Your password in Tabularia | choose one: 12 characters or more, not a common one |

**Windows (PowerShell)**

Command A, the Fernet key (44 characters, ending with `=`):

```powershell
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b).Replace('+','-').Replace('/','_')
```

Command B, a 64-character code (run it twice: once for `jwtSecret`, once for
`dbPassword`):

```powershell
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
```

**macOS and Linux**

```bash
openssl rand -base64 32 | tr '+/' '-_'
```

```bash
openssl rand -hex 32
```

> **Keep the Fernet key.** If it changes, the passwords saved in Tabularia's connections
> can no longer be read and must be typed again.

## Step 6 — Start PostgreSQL and Valkey

These two programs hold Tabularia's own records and its job queue. Create their space
in minikube, give PostgreSQL its password (the `dbPassword` of step 5), and start them:

```bash
kubectl create namespace tabularia-deps
```

```bash
kubectl create secret generic postgres --namespace tabularia-deps --from-literal=password=<dbPassword>
```

```bash
kubectl apply -f infrastructure/minikube/dependencies.yaml
```

**Check:** after a minute, `kubectl get pods --namespace tabularia-deps` shows two lines,
`postgres-…` and `valkey-…`, both with READY `1/1` and STATUS `Running`.

## Step 7 — Write your settings

Copy the example settings file (the same command works on all systems):

```bash
cp infrastructure/minikube/values.example.yaml my-values.yaml
```

Open `my-values.yaml` in a plain text editor:
- **Windows**: `notepad my-values.yaml`
- **macOS**: `open -e my-values.yaml`
- **Linux**: your usual editor.

Replace every `<…>`: the bucket of step 4, the keys of step 5, your email. Keep the
double quotes around each value. For example:

```yaml
    endpoint: "<https://s3.fr-par.scw.cloud>"
```

becomes

```yaml
    endpoint: "https://s3.fr-par.scw.cloud"
```

Leave the lines that start with `#` as they are for now: they are the optional parts of
steps 9 and 10.

> **Spaces matter in this file.** Do not change how far each line starts from the left,
> and never use the Tab key in it.

## Step 8 — Install Tabularia and open it

Install it:

```bash
helm install tabularia infrastructure/helm/tabularia --namespace tabularia --create-namespace --values my-values.yaml
```

Helm answers with a few lines starting with `NAME: tabularia` and `STATUS: deployed`.
minikube now downloads Tabularia's programs, about 1.5 GB, and starts them. Follow it:

```bash
kubectl get pods --namespace tabularia --watch
```

Wait until every line shows READY `1/1` and STATUS `Running` (5 to 15 minutes the
first time), then press **Ctrl+C** to stop watching.

**Open the door from your browser to Tabularia.** This command keeps running while you
use Tabularia; leave its window open:

```bash
kubectl port-forward --namespace ingress-nginx service/ingress-nginx-controller 8080:80
```

Open **http://localhost:8080** in your browser and sign in with the `adminEmail` and
`adminPassword` of `my-values.yaml`.

Tabularia is installed. The [getting started walkthrough](../getting-started-docker-compose.md)
shows how to upload a file of your own and build a first flow (its sections 6b and 7):
the screens are the same.

## Step 9 — Optional: run flows on ClickHouse

Tabularia can send the work of a flow to your ClickHouse service instead of doing it on
your computer. ClickHouse then reads and writes the files of your bucket directly, so
the service should be in the same cloud and region as the bucket.

You need:
- a ClickHouse service reachable over the internet with HTTPS (port 8443);
- a ClickHouse user and its password.

**1. In ClickHouse**, in your service's SQL console, as its administrator user:

```sql
CREATE DATABASE IF NOT EXISTS tabularia;
CREATE NAMED COLLECTION tabularia_s3 AS
    access_key_id = '<access key of the bucket>',
    secret_access_key = '<secret key of the bucket>';
```

The *named collection* keeps the bucket's keys inside ClickHouse, so they never travel
in the queries Tabularia sends. If the user Tabularia signs in with is not the
administrator, also run `GRANT NAMED COLLECTION ON tabularia_s3 TO <clickhouse-user>;`.

**2. In `my-values.yaml`**, remove the `# ` at the start of the `clickhouse:` block and of
the `clickhousePassword` line, keeping the spaces after it so each line starts where its
neighbours do. Fill in the host, the user, the password, and as `s3Endpoint` the same
address as `storage.endpoint`. Then apply the change:

```bash
helm upgrade tabularia infrastructure/helm/tabularia --namespace tabularia --values my-values.yaml
```

**3. In Tabularia**, after a minute: **Administration → Engines** lists ClickHouse.
Choose it as your **Preferred engine** in **Settings**, or as the engine of a flow.

**Check:** with ClickHouse as your preferred engine, open a datasource in the **Viewer**:
its rows appear.

## Step 10 — Optional: Python and R in flows

A Script node runs Python or R code as a step of a flow, in an environment your
administrator prepares. On minikube it needs the 12 GB of step 2.

**1. A place for the environments' images.** minikube has one built in:

```bash
minikube addons enable registry
```

```bash
kubectl get service registry --namespace kube-system -o jsonpath="{.spec.clusterIP}"
```

The second command prints an address such as `10.101.227.179`.

**2. In `my-values.yaml`**, remove the `# ` at the start of the `scripts:` block (keeping
the spaces after it), put that address in `repository` (for example
`"10.101.227.179:80/tabularia"`), then apply:

```bash
helm upgrade tabularia infrastructure/helm/tabularia --namespace tabularia --values my-values.yaml
```

**3. A second bucket, for the scripts only.** In your cloud console, create a bucket
such as `tabularia-yourcompany-scripts`, and a key that can read, write and delete
objects in it but not list it. Add a rule that deletes its objects after one day.
[The scripts bucket](../scripting.md#the-scripts-bucket) explains why and shows the
policies for each provider.

**4. In Tabularia**, **Administration → Scripting environments**:
1. fill in the scripts bucket (endpoint, region, bucket, the two keys) and press
   **Test**;
2. create an environment, for example Python 3.12 with the packages your scripts need;
3. wait until it is **Ready**: a few minutes for Python, ten or more for R, whose
   packages are compiled.

Then add a **Script** node to a flow. [Python and R in flows](../scripting.md) explains the
node, the editor and the variables. You can also keep a script on its own in a folder,
with its versions: **Explore → New → Script (Python or R)**.

A Python environment also runs **marimo notebooks**, apps your colleagues open in the
browser: **Explore → New → Notebook (marimo)**. They open from an address of their own,
`http://notebooks.localhost:8080`, which the `scripts:` block already names: your browser
finds it on your computer by itself, through the same `port-forward`. If you changed the
port of the `port-forward`, change it in `origin` too. [marimo notebooks as
apps](../capabilities/notebooks.md) explains them, and why the separate address.

> The registry's address changes if you turn the add-on off and on again. Then put the
> new address in `my-values.yaml` and run the `helm upgrade` of point 2 again.

> The chart also adds a network rule that keeps the scripts and the image builds away
> from Tabularia's own pods and from the cloud's metadata address. minikube's network
> does not apply such rules: here the rule is written but has no effect. A company
> cluster applies it; [the kind guide](kind.md) builds one on your computer.

## Every day

**Stop**: press Ctrl+C in the window of the `port-forward`, then:

```bash
minikube stop
```

Nothing is lost: Tabularia's records stay on minikube's disk, your data in the bucket.

**Start again**: open Docker Desktop, then:

```bash
minikube start
```

Wait for the pods as in step 8 (`kubectl get pods --namespace tabularia`), run the
`port-forward` command again, and open http://localhost:8080.

**See what runs**: **Administration → Cluster** in Tabularia shows its pods, and the
scripts and containers while they run, refreshed every three seconds: their state,
restarts, CPU and memory, and the warning events. CPU and memory need minikube's
metrics-server, turned on once:

```bash
minikube addons enable metrics-server
```

**Install a newer release**: back up the database first, then download and unzip the
release as in step 3, copy `my-values.yaml` into the new folder, open the terminal there,
and run:

```bash
helm upgrade tabularia infrastructure/helm/tabularia --namespace tabularia --values my-values.yaml --wait --timeout 20m
```

Then reload the page in the browser with **Ctrl+Shift+R** (macOS: **Cmd+Shift+R**): a
tab left open keeps running the old version. The backup, the checks after the upgrade
and the way back are in [Upgrading Tabularia](upgrading.md).

**Remove everything**:

```bash
minikube delete
```

This deletes minikube with Tabularia, PostgreSQL and Valkey. Your buckets stay in your
cloud: delete them from your provider's console if you no longer need them.

## When something goes wrong

**See the state of everything:**

```bash
kubectl get pods --all-namespaces
```

A healthy installation shows READY `1/1` and STATUS `Running` on every line of the
namespaces `tabularia` and `tabularia-deps`. The table below starts from what you see.

**`minikube start` fails**
- *"Virtualization is not enabled"* or *"WSL 2 is not installed"* (Windows): turn on
  virtualization in the computer's BIOS settings (ask IT), or run `wsl --install` in a
  PowerShell opened as administrator, restart, and try again.
- *"Docker Desktop has only … memory"* or *"more than your system limit"*: Docker has less
  memory than `--memory` asks. Raise Docker's memory (step 2) or ask minikube for less.
- *Docker is not running*: open Docker Desktop and wait until it is ready.

**`helm install` stops with an error**
- *"secrets.… is required"*: that value of `my-values.yaml` is empty.
- *"yaml: line N: …"*: the file's spacing is broken around line N: a Tab, a missing
  quote, or a line moved left or right. Compare it with `values.example.yaml`.
- *"cannot reuse a name that is still in use"*: Tabularia is already installed. Use
  `helm upgrade` (same command, `upgrade` instead of `install`, without
  `--create-namespace`).

**A pod stays `Pending`**: minikube has no memory left for it. See why:

```bash
kubectl describe pods --namespace tabularia
```

*"Insufficient memory"* near the end means minikube is too small. Run `minikube delete`,
start again from step 2 with more `--memory`, and repeat steps 6 and 8.

**A pod shows `ErrImagePull` or `ImagePullBackOff`**: minikube cannot download it. A
company VPN or proxy often blocks it: disconnect the VPN, or ask IT to allow
`registry-1.docker.io`. Then wait: minikube retries on its own.

**A pod shows `CrashLoopBackOff`, or stays at READY `0/1`**: it starts and stops. Copy
its full name from `kubectl get pods --namespace tabularia` and read why:

```bash
kubectl logs <pod-name> --namespace tabularia --previous
```

(if it answers that there is no previous container, run it again without `--previous`).
The last lines say what is wrong, for example *"Configuration NOT safe for production:
JWT__SECRET is too short"*. It is usually a value of `my-values.yaml`:
- a `<…>` placeholder left in place;
- the Fernet key is not the 44 characters of command A;
- `jwtSecret` is shorter than 32 characters;
- `adminPassword` is shorter than 12 characters or too common;
- `dbPassword` is not the password given to PostgreSQL in step 6.

Fix `my-values.yaml` and run the `helm upgrade` command of the "Every day" section. While
a new pod fails, the previous one, if there is one, keeps Tabularia running: your change
is in effect only when every pod shows `1/1` again.

**The browser cannot open http://localhost:8080**
- The `port-forward` window was closed, or minikube stopped: start them again.
- *"address already in use"* in the `port-forward` window: another program uses port 8080.
  Use `8081:80` in the command and open http://localhost:8081 (with notebooks, write
  `http://notebooks.localhost:8081` in `origin` and run the `helm upgrade` again).

**Signing in fails**: use the `adminEmail` and `adminPassword` of `my-values.yaml`. The
administrator is created on the very first start only: changing `adminPassword`
afterwards does not change it. Once signed in, change your password from **Settings**.

**Uploads or previews fail with a storage error** (*403*, *Access Denied*,
*SignatureDoesNotMatch*, *NoSuchBucket*): check in `my-values.yaml` the bucket's endpoint,
region and name, and both keys; on Scaleway, the key's preferred project (step 4).
After a change, run `helm upgrade`.

**After an upgrade the page looks or behaves like before**: reload with **Ctrl+Shift+R**
(macOS: **Cmd+Shift+R**).

**ClickHouse is missing from Administration → Engines**: the `clickhouse:` block of
`my-values.yaml` is still commented out, or `helm upgrade` was not run after editing it.

**Flows on ClickHouse fail reading the data** (*Access Denied*, *named collection does not
exist*): check the named collection of step 9 and its `GRANT`, and that `s3Endpoint` is the
bucket's address as seen from the ClickHouse service.

**Scripting environments**
- *Scripting environments is greyed out in Administration*: the `scripts:` block of
  `my-values.yaml` is still commented out, or `helm upgrade` was not run.
- *An environment stays "Building" for a long time*: R environments compile their
  packages; ten to twenty minutes is normal.
- *"The image build ran out of memory"*: minikube is too small for the build; start
  minikube with more `--memory` (step 2).
- *Scripts fail because their image cannot be downloaded*: the registry's address
  changed (step 10, last note).
