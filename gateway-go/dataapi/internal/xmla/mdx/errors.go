package mdx

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// Stable error codes. A caller may switch on them; the message is for people.
const (
	CodeSyntax               = "syntax"                // malformed text: brackets, commas, trailing text, bad characters
	CodeEmpty                = "empty"                 // no statement at all
	CodeWithMember           = "with_member"           // WITH MEMBER
	CodeWithSet              = "with_set"              // WITH SET
	CodeWithOther            = "with_other"            // any other WITH clause
	CodeCreate               = "create"                // CREATE ...
	CodeDrillthrough         = "drillthrough"          // DRILLTHROUGH
	CodeCall                 = "call"                  // CALL
	CodeUnsupportedStatement = "unsupported_statement" // a statement that is not a SELECT
	CodeUnknownFunction      = "unknown_function"      // a function (or method call) outside the subset; Name is the function
	CodeUnsupportedMember    = "unsupported_member"    // a member property such as .CurrentMember; Name is the property
	CodeBadArguments         = "bad_arguments"         // wrong count or kind of arguments; Name is the function
	CodeArithmetic           = "arithmetic"            // + - / or arithmetic with *; Name is the operator
	CodeRange                = "range"                 // a:b ranges
	CodeFilterCondition      = "filter_condition"      // a Filter condition outside "measure <op> number [AND|OR ...]"
	CodeNoFrom               = "no_from"               // SELECT without FROM
	CodeAxisDuplicate        = "axis_duplicate"        // the same axis written twice
	CodeUnexpectedLiteral    = "unexpected_literal"    // a number or string where a set or member is needed
	CodeTooDeep              = "too_deep"              // nesting beyond MaxDepth
	CodeTooLong              = "too_long"              // text beyond MaxStatementBytes
)

// AllCodes lists every code, for documentation and tests.
var AllCodes = []string{
	CodeSyntax, CodeEmpty, CodeWithMember, CodeWithSet, CodeWithOther, CodeCreate,
	CodeDrillthrough, CodeCall, CodeUnsupportedStatement, CodeUnknownFunction,
	CodeUnsupportedMember, CodeBadArguments, CodeArithmetic, CodeRange,
	CodeFilterCondition, CodeNoFrom, CodeAxisDuplicate,
	CodeUnexpectedLiteral, CodeTooDeep, CodeTooLong,
}

// Error says which construct was not understood and where. Offset is a byte
// offset into the statement; Line and Column are 1-based (Column counts runes).
type Error struct {
	Code   string
	Name   string // the function, keyword or operator at fault, when there is one
	Msg    string // an English sentence, ready for a SOAP fault
	Offset int
	Line   int
	Column int
}

func (e *Error) Error() string {
	return fmt.Sprintf("%s (line %d, column %d)", e.Msg, e.Line, e.Column)
}

func newError(src, code, name string, off int, format string, args ...any) *Error {
	if off < 0 {
		off = 0
	}
	if off > len(src) {
		off = len(src)
	}
	head := src[:off]
	line := 1 + strings.Count(head, "\n")
	col := utf8.RuneCountInString(head[strings.LastIndexByte(head, '\n')+1:]) + 1
	return &Error{Code: code, Name: name, Msg: fmt.Sprintf(format, args...), Offset: off, Line: line, Column: col}
}
