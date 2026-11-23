package bayesexpr

import "fmt"

// Code is the id of an error. The browser chooses the message by it, in five languages
// (namespace bayes.expr), with the error's Args.
type Code string

// The codes of the lexer and the parser (lot E0). The type checker adds its own.
const (
	// Before the lexer.
	CodeTooLong  Code = "expr.too_long"  // more than max_expression_bytes; at 0, of length 0
	CodeNonASCII Code = "expr.non_ascii" // a byte >= 0x80; args: codepoint, and char, ascii for a confusable
	CodeControl  Code = "expr.control"   // a control byte other than tab, line feed, carriage return; args: codepoint

	// The lexer.
	CodeNumberForm      Code = "expr.number_form"      // args: kind (suffix, trailing_dot, leading_dot, second_dot, exponent)
	CodeNumberRange     Code = "expr.number_range"     // args: reason (chars, magnitude)
	CodeNumberUnderflow Code = "expr.number_underflow" // a non-zero literal read as 0
	CodeNameCase        Code = "expr.name_case"        // args: name (the word in lower case)
	CodeNameLong        Code = "expr.name_long"        // more than max_name_bytes
	CodeNameReserved    Code = "expr.name_reserved"    // ends in "__"
	CodeComment         Code = "expr.comment"          // "//" or "/*"
	CodeHash            Code = "expr.hash"             // "#"
	CodeText            Code = "expr.text"             // a quote
	CodeHint            Code = "expr.hint"             // args: hint (the message id of language.hints), token
	CodeSymbol          Code = "expr.symbol"           // args: char

	// The parser.
	CodeEmpty       Code = "expr.empty"
	CodeUnexpected  Code = "expr.unexpected"   // args: expected (see the expect* constants)
	CodeUnclosed    Code = "expr.unclosed"     // args: bracket ("(" or "[")
	CodeCallEmpty   Code = "expr.call_empty"   // args: name
	CodeTooDeep     Code = "expr.too_deep"     // max_expression_nesting
	CodeTooBig      Code = "expr.too_big"      // max_expression_nodes
	CodeLagArgument Code = "expr.lag_argument" // lag takes a bare name and a whole number written in digits
)

// syntaxCodes are the codes Parse can return.
var syntaxCodes = []Code{
	CodeTooLong, CodeNonASCII, CodeControl,
	CodeNumberForm, CodeNumberRange, CodeNumberUnderflow, CodeNameCase, CodeNameLong,
	CodeNameReserved, CodeComment, CodeHash, CodeText, CodeHint, CodeSymbol,
	CodeEmpty, CodeUnexpected, CodeUnclosed, CodeCallEmpty, CodeTooDeep, CodeTooBig, CodeLagArgument,
}

// What an expr.unexpected error says was expected (its "expected" argument).
const (
	expectValue     = "value"      // a number, a name, "(" or a unary "-"
	expectOperator  = "operator"   // an operator or the end of the expression
	expectParen     = "paren"      // an operator or ")"
	expectArgument  = "argument"   // an operator, "," or ")"
	expectIndexName = "index_name" // the name of a group index, inside brackets
	expectBracket   = "bracket"    // "]"
)

// Error is the first error of an expression. Pos and Len count code points of the input; every
// error but expr.too_long is at or before the first non-ASCII character, where a code point is a
// byte.
type Error struct {
	Code Code              `json:"code"`
	Pos  int               `json:"pos"`
	Len  int               `json:"len"`
	Args map[string]string `json:"args,omitempty"`
}

func (e *Error) Error() string {
	if len(e.Args) == 0 {
		return fmt.Sprintf("%s at %d+%d", e.Code, e.Pos, e.Len)
	}
	return fmt.Sprintf("%s at %d+%d %v", e.Code, e.Pos, e.Len, e.Args)
}
