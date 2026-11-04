package aisvc

import "context"

// LintIn is what /internal/script/lint checks (routes/internal_script.py): one Python file of a
// Script node, and the modules its environment has.
type LintIn struct {
	Path            string   `json:"path"`
	Content         string   `json:"content"`
	LanguageVersion *string  `json:"language_version"`
	CheckImports    bool     `json:"check_imports"` // the installed set is not empty
	Installed       []string `json:"installed"`
	Local           []string `json:"local"`
	Environment     string   `json:"environment"`
}

// Diagnostic is one of Ruff's findings, or a module not installed (script_lint).
type Diagnostic struct {
	Line      any    `json:"line"`
	Column    any    `json:"column"`
	EndLine   any    `json:"end_line"`
	EndColumn any    `json:"end_column"`
	Code      string `json:"code"`
	Message   string `json:"message"`
	Severity  string `json:"severity"`
}

// Lint is script_lint.lint_python and, with CheckImports, missing_imports, merged in the order
// of the code.
func (c *Client) Lint(ctx context.Context, in LintIn) ([]Diagnostic, error) {
	if c == nil {
		return nil, ErrNoClient
	}
	if in.Installed == nil {
		in.Installed = []string{}
	}
	if in.Local == nil {
		in.Local = []string{}
	}
	var out struct {
		Diagnostics []Diagnostic `json:"diagnostics"`
	}
	if err := c.post(ctx, "/internal/script/lint", in, &out); err != nil {
		return nil, err
	}
	return out.Diagnostics, nil
}
