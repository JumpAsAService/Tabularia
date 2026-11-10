"""Reads the Go gateway's code the way drift.py and codes.py read the Python one with `ast`:
the value of a package-level literal (`var x = map[string][]string{…}`, a constant) and the
places that raise a dbt export refusal (`refuse(message, code, "key", value, …)` and the
`Error{Code: "…", Params: pyjson.Object{{Key: "…"}, …}}` literals). No Go toolchain needed: a
small tokenizer, enough for literals made of strings, numbers, booleans and nested braces.
usage (a look at one value): python3 golit.py <file.go> <name>"""
import ast, re, sys

_TOKEN = re.compile(r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<string>"(?:[^"\\\n]|\\.)*")
  | (?P<raw>`[^`]*`)
  | (?P<rune>'(?:[^'\\\n]|\\.)+')
  | (?P<number>\d[\d_]*(?:\.\d+)?)
  | (?P<ident>[A-Za-z_]\w*)
  | (?P<op>:=|\.\.\.|[{}()\[\],:;=.*&<>!+\-/|%^])
  | (?P<space>\s+)
""", re.S | re.X)


class Token:
    __slots__ = ("kind", "text", "line")

    def __init__(self, kind, text, line):
        self.kind, self.text, self.line = kind, text, line

    def __repr__(self):
        return f"{self.kind}:{self.text}@{self.line}"


def tokens(source: str) -> list[Token]:
    out, pos, line = [], 0, 1
    while pos < len(source):
        m = _TOKEN.match(source, pos)
        if not m:
            raise SyntaxError(f"golit: cannot read the Go source at line {line}: {source[pos:pos + 40]!r}")
        kind = m.lastgroup
        if kind not in ("comment", "space"):
            out.append(Token(kind, m.group(), line))
        line += m.group().count("\n")
        pos = m.end()
    return out


def _string(t: Token) -> str:
    if t.kind == "raw":
        return t.text[1:-1]
    return ast.literal_eval(t.text)       # Go's escapes in a "…" string are Python's


class _Parser:
    def __init__(self, toks, i):
        self.t, self.i = toks, i

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else Token("eof", "", 0)

    def take(self, text=None):
        tok = self.peek()
        if text is not None and tok.text != text:
            raise SyntaxError(f"golit: expected {text!r}, found {tok.text!r} at line {tok.line}")
        self.i += 1
        return tok

    def skip_balanced(self, open_, close):
        depth = 0
        while True:
            tok = self.take()
            if tok.text == open_:
                depth += 1
            elif tok.text == close:
                depth -= 1
                if depth == 0:
                    return
            elif tok.kind == "eof":
                raise SyntaxError("golit: unbalanced")

    def type_(self):
        tok = self.peek()
        if tok.text == "map":
            self.take(); self.take("["); self.type_(); self.take("]"); self.type_()
        elif tok.text == "[":
            self.take()
            if self.peek().text != "]":
                self.take()
            self.take("]"); self.type_()
        elif tok.text == "*":
            self.take(); self.type_()
        elif tok.text == "struct":
            self.take(); self.skip_balanced("{", "}")
        elif tok.kind == "ident":
            self.take()
            if self.peek().text == ".":
                self.take(); self.take()
        else:
            raise SyntaxError(f"golit: not a type: {tok.text!r} at line {tok.line}")

    def value(self):
        tok = self.peek()
        if tok.text == "{":
            return self.composite()
        if tok.kind in ("string", "raw"):
            self.take()
            value = _string(tok)
            while self.peek().text == "+" and self.peek(1).kind in ("string", "raw"):     # "a" + "b"
                self.take(); value += _string(self.take())
            return value
        if tok.kind == "number":
            self.take()
            text = tok.text.replace("_", "")
            return float(text) if "." in text else int(text)
        if tok.text in ("true", "false", "nil"):
            self.take()
            return {"true": True, "false": False, "nil": None}[tok.text]
        if tok.text == "-" and self.peek(1).kind == "number":
            self.take()
            return -self.value()
        # a typed composite literal (`[]string{…}`, `pkg.Type{…}`): its type, then its elements
        self.type_()
        return self.composite()

    def composite(self):
        self.take("{")
        keyed, items = False, []
        while self.peek().text != "}":
            if self.peek().kind == "ident" and self.peek(1).text == ":":    # a struct field's name
                key = self.take().text; self.take(":")
                keyed = True
                items.append((key, self.value()))
            else:
                first = self.value()
                if self.peek().text == ":":
                    self.take(":")
                    keyed = True
                    items.append((first if not isinstance(first, list) else tuple(first), self.value()))
                else:
                    items.append((None, first))
            if self.peek().text == ",":
                self.take(",")
        self.take("}")
        return dict(items) if keyed else [v for _, v in items]


def value(source: str, name: str):
    """The value of the package-level `name = <literal>` (in a var or const block too)."""
    toks, depth = tokens(source), 0
    for i, t in enumerate(toks):
        if t.text == "{":
            depth += 1
        elif t.text == "}":
            depth -= 1
        elif depth == 0 and t.kind == "ident" and t.text == name and i + 1 < len(toks) and toks[i + 1].text == "=":
            return _Parser(toks, i + 2).value()
    raise KeyError(name)


def _arguments(toks, i):
    """The arguments of the call whose `(` is toks[i]: a list of token lists, and the index after `)`."""
    args, current, depth = [], [], 0
    while True:
        t = toks[i]
        if t.text in "([{" and t.kind == "op":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif t.text in ")]}" and t.kind == "op":
            depth -= 1
            if depth == 0:
                if current:
                    args.append(current)
                return args, i + 1
        if depth == 1 and t.text == ",":
            args.append(current)
            current = []
        else:
            current.append(t)
        i += 1


def raises(source: str) -> list[tuple[str | None, int, set]]:
    """Every refusal raised in the file: (code or None when not a literal, line, the keys of its params)."""
    toks, out = tokens(source), []
    for i, t in enumerate(toks):
        before = toks[i - 1].text if i else ""
        # refuse(…) of the exporter; not q.refuse(status, text), the HTTP answer of a route
        if t.text == "refuse" and t.kind == "ident" and before not in ("func", ".") and toks[i + 1].text == "(":
            args, _ = _arguments(toks, i + 1)
            code = _string(args[1][0]) if len(args) > 1 and len(args[1]) == 1 and args[1][0].kind in ("string", "raw") else None
            params = {_string(a[0]) for a in args[2::2] if len(a) == 1 and a[0].kind in ("string", "raw")}
            out.append((code, t.line, params))
        elif t.text == "Error" and t.kind == "ident" and before not in ("func", "type", "*") and toks[i + 1].text == "{":
            fields, _ = _arguments(toks, i + 1)
            code, params, found = None, set(), False
            for f in fields:
                if len(f) >= 3 and f[0].text == "Code" and f[1].text == ":":
                    found = True
                    if len(f) == 3 and f[2].kind in ("string", "raw"):
                        code = _string(f[2])
                elif f and f[0].text == "Params":
                    params = {_string(f[k + 2]) for k in range(len(f) - 2)
                              if f[k].text == "Key" and f[k + 1].text == ":" and f[k + 2].kind in ("string", "raw")}
            if found and code is not None:      # `Code: code` passes on a code raised elsewhere
                out.append((code, t.line, params))
    return out


if __name__ == "__main__":
    print(value(open(sys.argv[1]).read(), sys.argv[2]))
