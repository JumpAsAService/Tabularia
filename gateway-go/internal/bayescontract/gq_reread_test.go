package bayescontract_test

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
)

// The quantity manifest says, in `gq_reread` false with the Stan type, that generate_quantities cannot reread a variable from
// the 8 digits of CmdStan's CSV (the owner's decision of 2026-11-24); a manifest that says nothing, or one stored before, is as it was.
func TestAQuantityTheDrawsCannotBeRereadFor(t *testing.T) {
	no, typ := bayescontract.NoGQReread("cholesky_factor_corr")
	qs := []bayescontract.Quantity{{Stan: "a", Role: bayescontract.RoleQuantity, Dims: []bayescontract.Dim{}},
		{Stan: "L_x", Role: bayescontract.RoleInternal, Dims: []bayescontract.Dim{}, GQReread: no, GQRereadType: typ}}
	if got, ok := bayescontract.GQRereadRefusal(qs); !ok || got != "cholesky_factor_corr" {
		t.Errorf("got %q, %v", got, ok)
	}
	if _, ok := bayescontract.GQRereadRefusal(qs[:1]); ok {
		t.Error("a program with no such variable is refused")
	}
	b, err := json.Marshal(qs[1])
	if err != nil || !strings.Contains(string(b), `"gq_reread":false,"gq_reread_type":"cholesky_factor_corr"`) {
		t.Errorf("the manifest's bytes: %s (%v)", b, err)
	}
	var back bayescontract.Quantity
	if err := json.Unmarshal([]byte(`{"stan":"b","role":"quantity","dims":[]}`), &back); err != nil || back.GQReread != nil {
		t.Errorf("a manifest stored before has no word on it: %+v (%v)", back, err)
	}
	var found bool
	for _, r := range bayescontract.Refusals {
		found = found || (r.Code == "gq_reread" && r.Exit == 3 && len(r.Modes) == 1 && r.Modes[0] == bayescontract.ModeSample)
	}
	if !found {
		t.Error("the refusal gq_reread is not declared")
	}
}
