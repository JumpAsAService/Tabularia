# MLflow connections

An MLflow connection is a tracking server that lives **outside Tabularia**: the place where
the statistical models of a flow are recorded (experiments, runs, parameters, metrics, files
and registered versions). It names the server's address and the way to sign in. Tabularia
does not install or run MLflow. To try one on a Kubernetes cluster, see
[an optional MLflow server next to Tabularia](../deploy/kind.md#14-optional-a-tracking-server-for-the-statistical-models-mlflow).

## At a glance

| | |
|---|---|
| Where | A folder in Explore, **New → New connection**, type **MLflow** |
| Used by | The **MLflow** block of the [statistical model](../capabilities/models.md) nodes and the Predict node |
| Authentication | **None**, **User and password** (MLflow's basic-authentication app) or **Bearer token** (a reverse proxy or an identity provider in front of MLflow) |
| Permissions | CONNECT on the connection's folder, to create it and to choose it in a node |

## The fields

| Field | Meaning |
|---|---|
| Tracking URI | `https://mlflow.example.com`, with its path if the server has one (`https://host/mlflow`). No user, password, query or fragment in it. |
| Authentication | None, User and password, or Bearer token. |
| User | For User and password. |
| Password or token | Encrypted, and never shown again. Editing, an empty field keeps the stored one. |
| Verify the server's certificate | On by default. Off only on a network you trust: the credentials would go to a server nobody vouched for, and the audit log marks the connection. |
| Certificate authority | Optional, with the verification on: your company's own authority (PEM), if it signed the server's certificate. |
| Address for the browser | Optional: when people open MLflow at another address than the one Tabularia uses. |

The secret goes **only to the tracking server's address**: a redirect to another address is
not followed, and a new address, user or kind of authentication needs the secret typed
again before the connection can be saved.

Tabularia never calls the loopback, a link-local address (the clouds' metadata service),
a multicast or an unspecified address, whatever name leads to it: the check is made on the
address the name resolves to, at the moment of connecting. A tracking server on a private
network or in the cluster (`mlflow.mlflow.svc`, `10.x`, `192.168.x`) is the ordinary case and
is allowed.

## Test connection

The test only **reads**, with the connection's credentials, before or after saving:

| Check | Call (MLflow REST API 2.0) | It tells you |
|---|---|---|
| Credentials | `POST /api/2.0/mlflow/experiments/search` with `max_results = 1` | The server is MLflow and the credentials work. This one decides the result. |
| Version | `GET /version` | The server's version, when it says it. |
| Model registry | `GET /api/2.0/mlflow/registered-models/search?max_results=1` | Models can be registered. A server without a database backend has no registry: runs can still be logged. |
| Artifact proxy | `GET /api/2.0/mlflow-artifacts/artifacts` | Files can be uploaded through the server. Without it, only parameters, metrics and tags can be logged. |

A failure is named: the address cannot be reached, no answer in time, a certificate not
signed by a trusted authority or not valid for the address, wrong credentials (401), not
allowed (403), no MLflow API at the address (404), a redirect to another address, or an
answer that is not MLflow's. The server's own text is never shown.

The calls are the ones of MLflow's REST reference (API 2.0). Tabularia's own tests run against a
stand-in of that API, and it has been tried against a real server, MLflow 2.22.0, with its basic
authentication, its artifact proxy and a model registry on SQLite.

## What a model records here

A run of a flow whose model node names this connection writes to the experiment the node names
(created when missing): the node's parameters and the model's formula, every number of the fit
and the statistic and p-value of each assumption as metrics, tags that say which flow, node and
version of the model made it, and, when the server has an artifact proxy, the results, the
coefficients, the report as PDF and the model with its card (for XGBoost and CatBoost, also the
model's own file, `xgboost.json` or `catboost.cbm`, never a pickle; the learning curves are
recorded as metric histories, one point a tree). With **Register the model as**, each
run adds a version of that registered model, which the **Predict** node can apply. The editor's
**Run** logs nothing. The connection's secret is used only by the gateway: it never reaches the
Job that fits the model. The details, and **Continue without MLflow**, are in
[Statistical models: MLflow](../capabilities/models.md#mlflow).

See [Connections](README.md) for what every type has in common.
