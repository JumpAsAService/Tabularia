package reportdef

import (
	"bytes"
	"encoding/base64"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strings"
	"unicode/utf8"
)

const svgNS = "http://www.w3.org/2000/svg"

// svgElements are the SVG elements that draw: anything else is refused.
var svgElements = map[string]bool{}

func init() {
	for _, e := range strings.Fields(`svg g defs symbol use title desc metadata style path rect circle ellipse
		line polyline polygon text tspan textPath linearGradient radialGradient stop clipPath mask pattern image
		marker filter feGaussianBlur feOffset feBlend feColorMatrix feComposite feFlood feMerge feMergeNode
		feMorphology feDropShadow feComponentTransfer feFuncR feFuncG feFuncB feFuncA switch`) {
		svgElements[e] = true
	}
}

// editorNS are the namespaces of drawing programs' notes, which no renderer draws.
var editorNS = map[string]bool{
	"http://www.inkscape.org/namespaces/inkscape": true, "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd": true,
	"http://www.w3.org/1999/02/22-rdf-syntax-ns#": true, "http://creativecommons.org/ns#": true,
	"http://purl.org/dc/elements/1.1/": true, "http://www.bohemiancoding.com/sketch/ns": true, "http://www.serif.com/": true,
}

var (
	urlRe      = regexp.MustCompile(`(?i)url\s*\(\s*(['"]?)\s*([^)'"]*)`)
	spaces     = regexp.MustCompile(`[\s\x{1c}-\x{1f}\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+`)
	imageHref  = regexp.MustCompile(`^data:image/(png|jpeg);base64,`)
	dataURLPy  = regexp.MustCompile(`^data:image/(png|jpeg|svg\+xml);base64,([A-Za-z0-9+/=\s\x{1c}-\x{1f}\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+)\n?$`)
	base64Text = regexp.MustCompile(`^[A-Za-z0-9+/]*={0,2}$`)
)

// unsafeText is _unsafe_text: what makes a style or an attribute's value reach outside the image.
func unsafeText(text string) string {
	low := strings.ToLower(spaces.ReplaceAllString(text, ""))
	if strings.Contains(low, "javascript:") || strings.Contains(low, "@import") || strings.Contains(low, "expression(") {
		return "a script or an import"
	}
	for _, m := range urlRe.FindAllStringSubmatch(text, -1) {
		if !strings.HasPrefix(strings.TrimSpace(m[2]), "#") {
			return "a url() outside the image"
		}
	}
	return ""
}

var errMalformed = errors.New("not well-formed")

// SVGProblem is svg_problem: why an SVG is not safe to keep, or "". Refused, never cleaned.
// ElementTree's parser is expat; encoding/xml in strict mode refuses what expat refuses for
// the SVGs an editor writes (an undeclared entity, tags that do not close, a second root).
func SVGProblem(data []byte) string {
	if !utf8.Valid(data) {
		return "it is not UTF-8 text"
	}
	head := strings.ToLower(string(data))
	if strings.Contains(head, "<!doctype") || strings.Contains(head, "<!entity") {
		return "it declares a DOCTYPE or entities"
	}
	if strings.Contains(head, "<?xml-stylesheet") {
		return "it links a stylesheet"
	}
	type element struct {
		ns, local string
		attrs     []xml.Attr
		text      string
	}
	var found []*element
	d := xml.NewDecoder(bytes.NewReader(data))
	d.Strict = true
	depth, roots := 0, 0
	var open []*element
	var textOf *element // the element whose `text` is still being read
	for {
		tok, err := d.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return "it is not well-formed XML"
		}
		switch t := tok.(type) {
		case xml.StartElement:
			if depth == 0 {
				roots++
				if roots > 1 {
					return "it is not well-formed XML"
				}
			}
			e := &element{ns: t.Name.Space, local: t.Name.Local}
			for _, a := range t.Attr {
				if a.Name.Space == "xmlns" || (a.Name.Space == "" && a.Name.Local == "xmlns") {
					continue
				}
				e.attrs = append(e.attrs, a)
			}
			found = append(found, e)
			open = append(open, e)
			textOf = e
			depth++
		case xml.EndElement:
			depth--
			open = open[:len(open)-1]
			textOf = nil
		case xml.CharData:
			if depth == 0 {
				if strings.TrimSpace(string(t)) != "" {
					return "it is not well-formed XML"
				}
			} else if textOf != nil {
				textOf.text += string(t)
			}
		case xml.Comment, xml.ProcInst:
			if textOf != nil && depth > 0 {
				// ElementTree's text stops at a comment: what follows is the comment's tail
				textOf = nil
			}
		}
	}
	if roots == 0 {
		return "it is not well-formed XML"
	}
	root := found[0]
	if !(root.local == "svg" && (root.ns == svgNS || root.ns == "")) {
		return "it is not an SVG"
	}
	for _, el := range found {
		if editorNS[el.ns] {
			continue
		}
		if (el.ns != "" && el.ns != svgNS) || !svgElements[el.local] {
			return fmt.Sprintf("it holds a <%s> element", el.local)
		}
		if el.local == "style" {
			if why := unsafeText(el.text); why != "" {
				return "its style holds " + why
			}
		}
		for _, a := range el.attrs {
			attr := strings.ToLower(a.Name.Local)
			if strings.HasPrefix(attr, "on") {
				return fmt.Sprintf("it has an event attribute (%s)", attr)
			}
			if attr == "href" {
				v := strings.TrimSpace(a.Value)
				if !(strings.HasPrefix(v, "#") || (el.local == "image" && imageHref.MatchString(v))) {
					return "it refers to something outside the image"
				}
			}
			if why := unsafeText(a.Value); why != "" {
				return "an attribute holds " + why
			}
		}
	}
	return ""
}

// CheckImage is check_image: the image as it is kept, its data URL with the base64 in one piece.
func CheckImage(value any, where, item string) (string, error) {
	bad := func(message string) error {
		return problem(fmt.Sprintf("The image «%s»: %s", item, message), where, &item)
	}
	s, ok := value.(string)
	if !ok || s == "" {
		return "", bad("upload a PNG, a JPEG or an SVG")
	}
	m := dataURLPy.FindStringSubmatch(s)
	if m == nil {
		return "", bad("an image is a PNG, a JPEG or an SVG uploaded into the report (no links)")
	}
	encoded := spaces.ReplaceAllString(m[2], "")
	if !base64Text.MatchString(encoded) {
		return "", bad("it is not valid base64")
	}
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return "", bad("it is not valid base64")
	}
	if len(data) > MaxImageBytes {
		return "", bad(fmt.Sprintf("it is %d KiB: at most %d KiB", len(data)/1024, MaxImageBytes/1024))
	}
	kind := m[1]
	if kind == "png" && !bytes.HasPrefix(data, []byte("\x89PNG\r\n\x1a\n")) {
		return "", bad("it is not a PNG")
	}
	if kind == "jpeg" && !bytes.HasPrefix(data, []byte("\xff\xd8\xff")) {
		return "", bad("it is not a JPEG")
	}
	if kind == "svg+xml" {
		why := SVGProblem(data)
		// how the engine knows an SVG (backend/app/reports/definition.py image_bytes)
		if why == "" {
			first := data[:min(1024, len(data))]
			start := bytes.ToLower(bytes.TrimLeft(first, " \t\n\r\x0b\x0c"))
			if !(bytes.HasPrefix(start, []byte("<svg")) || bytes.HasPrefix(start, []byte("<?xml"))) ||
				!bytes.Contains(bytes.ToLower(data[:min(4096, len(data))]), []byte("<svg")) {
				why = "it does not start with <svg or <?xml"
			}
		}
		if why != "" {
			return "", bad("this SVG cannot be used: " + why + ". Export it again as a plain SVG, or as a PNG")
		}
	}
	return "data:image/" + kind + ";base64," + encoded, nil
}
