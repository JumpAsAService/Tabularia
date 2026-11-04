package openlineage

import (
	"errors"
	"io"
	"math"
	"regexp"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// yamlSafeLoad is PyYAML's yaml.safe_load as the library reads openlineage.yml: one document,
// mappings in their order (a key written twice: the last value, at the first place), and the
// plain scalars resolved by YAML 1.1's rules (yes/no/on/off are booleans, 0777 is octal, 1:30
// is sexagesimal, a float needs its dot), not by yaml.v3's YAML 1.2 ones.
func yamlSafeLoad(text string) (any, error) {
	dec := yaml.NewDecoder(strings.NewReader(text))
	var doc yaml.Node
	if err := dec.Decode(&doc); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, nil // an empty file: None
		}
		return nil, err
	}
	var more yaml.Node
	if err := dec.Decode(&more); err == nil {
		return nil, errors.New("expected a single document in the stream")
	} else if !errors.Is(err, io.EOF) {
		return nil, err
	}
	return yamlValue(&doc)
}

var (
	yamlBool  = regexp.MustCompile(`^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$`)
	yamlFloat = regexp.MustCompile(`^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$`)
	yamlInt   = regexp.MustCompile(`^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$`)
	yamlNull  = regexp.MustCompile(`^(?:~|null|Null|NULL|)$`)
)

func yamlValue(n *yaml.Node) (any, error) {
	switch n.Kind {
	case yaml.DocumentNode:
		if len(n.Content) == 0 {
			return nil, nil
		}
		return yamlValue(n.Content[0])
	case yaml.AliasNode:
		return yamlValue(n.Alias)
	case yaml.SequenceNode:
		out := []any{}
		for _, c := range n.Content {
			v, err := yamlValue(c)
			if err != nil {
				return nil, err
			}
			out = append(out, v)
		}
		return out, nil
	case yaml.MappingNode:
		out := pyjson.Object{}
		for i := 0; i+1 < len(n.Content); i += 2 {
			k, v := n.Content[i], n.Content[i+1]
			if k.Kind == yaml.ScalarNode && k.Value == "<<" && k.Style == 0 { // a merge key
				merged, err := yamlValue(v)
				if err != nil {
					return nil, err
				}
				var sources []any
				if l, ok := merged.([]any); ok {
					sources = l
				} else {
					sources = []any{merged}
				}
				for _, s := range sources {
					if o, ok := s.(pyjson.Object); ok {
						for _, kv := range o {
							if _, has := out.Get(kv.Key); !has {
								out = append(out, kv)
							}
						}
					}
				}
				continue
			}
			key, err := yamlValue(k)
			if err != nil {
				return nil, err
			}
			val, err := yamlValue(v)
			if err != nil {
				return nil, err
			}
			ks, ok := key.(string)
			if !ok {
				ks = pyjson.Str(key) // the library's config keys are texts; another key is kept as its text
			}
			out = out.Set(ks, val)
		}
		return out, nil
	}
	return yamlScalar(n)
}

func yamlScalar(n *yaml.Node) (any, error) {
	v := n.Value
	tag := ""
	if n.Style&yaml.TaggedStyle != 0 {
		tag = n.ShortTag()
	} else if n.Style&(yaml.DoubleQuotedStyle|yaml.SingleQuotedStyle|yaml.LiteralStyle|yaml.FoldedStyle) != 0 {
		return v, nil
	}
	switch {
	case tag == "!!str":
		return v, nil
	case tag == "!!null" || (tag == "" && yamlNull.MatchString(v)):
		return nil, nil
	case tag == "!!bool" || (tag == "" && yamlBool.MatchString(v)):
		switch strings.ToLower(v) {
		case "yes", "true", "on":
			return true, nil
		}
		return false, nil
	case tag == "!!int" || (tag == "" && yamlInt.MatchString(v)):
		return yamlParseInt(v)
	case tag == "!!float" || (tag == "" && yamlFloat.MatchString(v)):
		return yamlParseFloat(v)
	}
	return v, nil
}

func yamlParseInt(v string) (any, error) {
	s := strings.ReplaceAll(v, "_", "")
	sign := int64(1)
	if strings.HasPrefix(s, "-") {
		sign, s = -1, s[1:]
	} else if strings.HasPrefix(s, "+") {
		s = s[1:]
	}
	var n int64
	var err error
	switch {
	case s == "0":
		return int64(0), nil
	case strings.HasPrefix(s, "0b"):
		n, err = strconv.ParseInt(s[2:], 2, 64)
	case strings.HasPrefix(s, "0x"):
		n, err = strconv.ParseInt(s[2:], 16, 64)
	case strings.HasPrefix(s, "0"):
		n, err = strconv.ParseInt(s[1:], 8, 64)
	case strings.Contains(s, ":"):
		base := int64(1)
		parts := strings.Split(s, ":")
		for i := len(parts) - 1; i >= 0; i-- {
			d, e := strconv.ParseInt(parts[i], 10, 64)
			if e != nil {
				return nil, e
			}
			n += d * base
			base *= 60
		}
	default:
		n, err = strconv.ParseInt(s, 10, 64)
	}
	return sign * n, err
}

func yamlParseFloat(v string) (any, error) {
	s := strings.ToLower(strings.ReplaceAll(v, "_", ""))
	sign := 1.0
	if strings.HasPrefix(s, "-") {
		sign, s = -1, s[1:]
	} else if strings.HasPrefix(s, "+") {
		s = s[1:]
	}
	switch {
	case s == ".inf":
		return sign * math.Inf(1), nil
	case s == ".nan":
		return math.NaN(), nil
	case strings.Contains(s, ":"):
		base, n := 1.0, 0.0
		parts := strings.Split(s, ":")
		for i := len(parts) - 1; i >= 0; i-- {
			d, e := strconv.ParseFloat(parts[i], 64)
			if e != nil {
				return nil, e
			}
			n += d * base
			base *= 60
		}
		return sign * n, nil
	}
	f, err := strconv.ParseFloat(s, 64)
	return sign * f, err
}
