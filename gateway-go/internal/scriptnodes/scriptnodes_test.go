package scriptnodes

import (
	"reflect"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func TestSplitLinesIsPythons(t *testing.T) {
	if got := SplitLines("a\r\nb\rc\x1cd e\n"); !reflect.DeepEqual(got, []string{"a", "b", "c", "d", "e"}) {
		t.Errorf("%q", got)
	}
}

func TestTheChecksOfANode(t *testing.T) {
	node := func(data pyjson.Object) Node { return Node{{Key: "id", Value: "n"}, {Key: "data", Value: data}} }
	files := []any{pyjson.Object{{Key: "path", Value: "main.py"}, {Key: "content", Value: "open('s3://bkt/x')"}}}
	if r := CheckFiles(node(pyjson.Object{{Key: "files", Value: files}}), "python", "bkt", false); r == nil {
		t.Error("the bucket named in the code passed")
	}
	if r := CheckSettings(node(pyjson.Object{{Key: "retries", Value: true}}), -1); r != nil {
		t.Errorf("True is an int in Python: %v", r)
	}
	if r := CheckSettings(node(pyjson.Object{{Key: "timeoutSeconds", Value: 60.0}}), -1); r == nil {
		t.Error("a float timeout passed")
	}
}
