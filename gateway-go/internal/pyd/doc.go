// Package pyd answers a request's validation the way the Python gateway does:
// FastAPI 0.139 in front of pydantic 2.13 in lax mode. The 422s of the Go gateway
// must be byte-for-byte the Python ones, because clients (the interface, scripts,
// the MCP agent) read `detail[].loc` and `detail[].type` to say what is wrong.
//
// The pieces, in the order a request meets them:
//
//   - ReadBody: FastAPI's body reading. The content type decides whether the bytes
//     are JSON (json.loads, with CPython's messages and positions) or stay bytes;
//     a broken JSON is a 422 `json_invalid` BEFORE any dependency runs (so before
//     the authentication), an undecodable one a 400.
//   - Model / Type: a declaration of a pydantic model (fields, defaults, Field
//     constraints, Literal, Optional, unions, nested models, validators). The
//     gateway's own request bodies are generated from the Python models into
//     the gatewaymodels package (testdata/gen_models.py).
//   - Params: the request's path, query and body validated in FastAPI's order
//     (after the dependencies), the errors accumulated as `exc.errors()` lists
//     them. A valid body is a *ModelValue: Get, IsSet (model_fields_set), Dump
//     (model_dump(mode="json")).
//   - RenderErrors / Response: the 422 as the gateway writes it, with its fallback
//     (main.py `_invalid_request`) when a value cannot be written in JSON.
//
// The values are the ones json.loads builds: nil, bool, int64 or *big.Int,
// float64, string, []any and *Object (a dict, which keeps its keys' order).
// Strings hold lone UTF-16 surrogates as WTF-8, as Python's str can hold them.
//
// The proof is differential: testdata/run.sh asks the real FastAPI and pydantic
// of the gateway's image to answer thousands of requests (testdata/make_cases.py)
// and TestDifferential compares this package's answers with them, byte for byte.
package pyd
