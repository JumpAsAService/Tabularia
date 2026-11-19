# MLflow connections

An MLflow connection is a tracking server that lives **outside Tabularia**: the place where
the statistical models of a flow are recorded (experiments, runs, parameters, metrics, files
and registered versions). It names the server's address and the way to sign in. Tabularia
does not install or run MLflow.

## At a glance

| | |
|---|---|
| Where | A folder in Explore, **New → New connection**, type **MLflow** |
| Used by | The **MLflow** block of the model nodes and the Predict node (when they ship) |
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
stand-in of that API; the range of MLflow versions it supports is written here once it has been
run against a real server.

See [Connections](README.md) for what every type has in common.
