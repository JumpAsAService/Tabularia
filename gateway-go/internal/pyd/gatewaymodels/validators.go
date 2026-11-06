package gatewaymodels

import (
	"errors"
	"fmt"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
)

// The Go twins of the Python validators the generated models reference: each one
// says the same thing in the same words, because the text is the 422's msg.

// passwordMinLength and passwordMaxBytes are core/passwords.py's rule.
const (
	passwordMinLength = 12
	passwordMaxBytes  = 72
)

// knownPasswords is core/passwords.py KNOWN_PASSWORDS: compared stripped and
// lower-cased.
var knownPasswords = map[string]bool{
	"admin": true, "administrator": true, "change-me-in-production": true, "change-me-2026": true,
	"changeme": true, "change-me": true, "password": true, "tabularia": true, "admin123": true,
	"12345678": true, "123456789012": true,
}

// PasswordProblem is core/passwords.py password_problem: why this cannot be a new
// password, or "" when it can.
func PasswordProblem(p string) string {
	switch {
	case p == "":
		return "the password is empty"
	case knownPasswords[pyd.Lower(pyd.Strip(p))]:
		return "the password is a published one everyone can try"
	case pyd.Len(p) < passwordMinLength:
		return fmt.Sprintf("the password is too short (%d characters, at least %d are needed)", pyd.Len(p), passwordMinLength)
	case len(p) > passwordMaxBytes:
		return fmt.Sprintf("the password is longer than %d bytes, more than bcrypt reads", passwordMaxBytes)
	}
	return ""
}

// checkNewPassword is schemas/models.py _check_new_password (the NewPassword type).
func checkNewPassword(v any) (any, error) {
	if problem := PasswordProblem(v.(string)); problem != "" {
		return nil, errors.New(problem)
	}
	return v, nil
}

// noDotDot is schemas/models.py no_dot_dot: SFTP and S3 folders and patterns may
// not go up with '..'.
func noDotDot(v any) (any, error) {
	for _, part := range strings.Split(strings.ReplaceAll(v.(string), "\\", "/"), "/") {
		if part == ".." {
			return nil, errors.New("'..' is not allowed: write the folder you mean")
		}
	}
	return v, nil
}

// shortTemplates is PlaceholderPreviewQuery._short.
func shortTemplates(v any) (any, error) {
	obj := v.(*pyd.Object)
	for _, k := range obj.Keys() {
		t, _ := obj.Get(k)
		if pyd.Len(t.(string)) > 4000 || pyd.Len(k) > 64 {
			return nil, errors.New("template too long")
		}
	}
	return v, nil
}

// apiVariableComplete is ApiVariable._complete: a list has values, a range has a
// start, an end and a step.
func apiVariableComplete(mv *pyd.ModelValue) error {
	name := mv.Get("name").(string)
	switch mv.Get("type") {
	case "list":
		if len(mv.Get("values").([]any)) == 0 {
			return fmt.Errorf("the variable '%s' has no values", name)
		}
	case "range":
		if pyd.Strip(mv.Get("start").(string)) == "" || pyd.Strip(mv.Get("end").(string)) == "" ||
			pyd.Strip(mv.Get("step").(string)) == "" {
			return fmt.Errorf("the range '%s' needs a start, an end and a step", name)
		}
	}
	return nil
}
