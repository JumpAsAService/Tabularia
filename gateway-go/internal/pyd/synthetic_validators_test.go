package pyd_test

import (
	"errors"
	"fmt"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
)

// The Go twins of testdata/synthetic.py's validators.

func synCheck(v any) (any, error) {
	switch v.(string) {
	case "bad":
		return nil, errors.New("bad value")
	case "worse":
		return nil, errors.New(`it's "worse": \ ok`)
	}
	return v, nil
}

func synNoDotDot(v any) (any, error) {
	for _, part := range strings.Split(strings.ReplaceAll(v.(string), "\\", "/"), "/") {
		if part == ".." {
			return nil, errors.New("'..' is not allowed: write the folder you mean")
		}
	}
	return v, nil
}

func synShort(v any) (any, error) {
	obj := v.(*pyd.Object)
	for _, k := range obj.Keys() {
		t, _ := obj.Get(k)
		if pyd.Len(t.(string)) > 4 || pyd.Len(k) > 3 {
			return nil, errors.New("template too long")
		}
	}
	return v, nil
}

func synWhole(mv *pyd.ModelValue) error {
	if f := mv.Get("folder").(string); f == "boom" {
		return fmt.Errorf("the folder '%s' exploded", f)
	}
	return nil
}
