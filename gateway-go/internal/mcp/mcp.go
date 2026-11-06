// Package mcp is what routes/mcp.py answers without running a tool: the tools offered to
// a token (`tools/list`) and the server's instructions, as the Python gateway computes
// them (tools.json, written by gen_tools.py in the gateway's image: the tools' input
// schemas are pydantic's JSON schemas of their arguments). Running a tool
// (`tools/call`) is the agent's, in Python.
package mcp

import (
	_ "embed"
	"fmt"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

//go:embed tools.json
var raw []byte

var data = func() pyjson.Object {
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		panic("mcp: tools.json: " + err.Error())
	}
	return v.(pyjson.Object)
}()

// Tools is _tool_list(level): the tools a token of that level sees, in metadata-only mode
// or not, each {name, title, description, inputSchema, annotations}.
func Tools(level string, withheld bool) []any {
	key := level
	if level != "write" {
		key = "read"
	}
	if withheld {
		key += "_withheld"
	}
	v, _ := data.Get(key)
	return v.([]any)
}

// Instructions is _instructions(level).
func Instructions(level string, withheld, confirmInTabulariaOnly bool) string {
	all, _ := data.Get("instructions")
	if level != "write" {
		level = "read"
	}
	v, _ := all.(pyjson.Object).Get(fmt.Sprintf("%s|%d|%d", level, b2i(withheld), b2i(confirmInTabulariaOnly)))
	return v.(string)
}

func b2i(b bool) int {
	if b {
		return 1
	}
	return 0
}
