// Package notebookpod holds app/notebook_pod/tabularia.py, the `tabularia` module a notebook's
// pod imports (services/notebook_runtime.py POD_MODULE): the gateway puts it in the pod's
// ConfigMap. A copy of the Python gateway's file; the test checks they are the same.
package notebookpod

import _ "embed"

// Module is the module's text.
//
//go:embed tabularia.py
var Module string
