package pypkg

import (
	"errors"
	"regexp"
	"strconv"
	"strings"
)

// ErrInvalidWheel is packaging's InvalidWheelFilename.
var ErrInvalidWheel = errors.New("invalid wheel filename")

// Tag is packaging.tags.Tag, lower-cased.
type Tag struct{ Interpreter, ABI, Platform string }

// Wheel is parse_wheel_filename's name, version and tags.
type Wheel struct {
	Name    string
	Version *Version
	Tags    []Tag
}

var (
	wheelName = regexp.MustCompile(`^[\p{L}\p{N}_.]+$`)
	buildTag  = regexp.MustCompile(`^(\p{Nd}+)(.*)`)
)

// ParseWheelFilename is packaging.utils.parse_wheel_filename (packaging 26.3).
func ParseWheelFilename(filename string) (*Wheel, error) {
	if !strings.HasSuffix(filename, ".whl") {
		return nil, ErrInvalidWheel
	}
	filename = filename[:len(filename)-4]
	dashes := strings.Count(filename, "-")
	if dashes != 4 && dashes != 5 {
		return nil, ErrInvalidWheel
	}
	parts := strings.SplitN(filename, "-", dashes-1)
	if strings.Contains(parts[0], "__") || !wheelName.MatchString(parts[0]) {
		return nil, ErrInvalidWheel
	}
	v, err := Parse(parts[1])
	if err != nil {
		return nil, ErrInvalidWheel
	}
	if dashes == 5 && !buildTag.MatchString(parts[2]) {
		return nil, ErrInvalidWheel
	}
	tags, err := parseTag(parts[len(parts)-1])
	if err != nil {
		return nil, ErrInvalidWheel
	}
	return &Wheel{Name: CanonicalizeName(parts[0]), Version: v, Tags: tags}, nil
}

// parseTag is packaging.tags.parse_tag: a compressed tag set, every combination.
func parseTag(tag string) ([]Tag, error) {
	var components [][]string
	for _, c := range strings.Split(tag, "-") {
		parts := strings.Split(c, ".")
		for _, p := range parts {
			if p == "" {
				return nil, ErrInvalidWheel
			}
		}
		components = append(components, parts)
	}
	if len(components) != 3 {
		return nil, ErrInvalidWheel
	}
	for _, i := range components[0] {
		if !isIdentifier(i) {
			return nil, ErrInvalidWheel
		}
	}
	var out []Tag
	for _, i := range components[0] {
		for _, a := range components[1] {
			for _, p := range components[2] {
				out = append(out, Tag{strings.ToLower(i), strings.ToLower(a), strings.ToLower(p)})
			}
		}
	}
	return out, nil
}

// WheelFits is wheel_fits: a wheel the environment's image can install (this CPython or any
// Python 3; Linux with glibc, or any platform). The error is Python's ValueError of a «cp3»
// interpreter with the stable ABI (int("")): a 500 there too.
func WheelFits(filename string, python [2]int) (bool, error) {
	w, err := ParseWheelFilename(filename)
	if err != nil {
		return false, nil
	}
	major, minor := strconv.Itoa(python[0]), strconv.Itoa(python[1])
	for _, t := range w.Tags {
		if !(t.Platform == "any" || strings.HasPrefix(t.Platform, "manylinux") || strings.HasPrefix(t.Platform, "linux")) {
			continue
		}
		if t.Interpreter == "cp"+major+minor || t.Interpreter == "py"+major || t.Interpreter == "py"+major+minor {
			return true, nil
		}
		r := []rune(t.Interpreter)
		if t.ABI == "abi3" && strings.HasPrefix(t.Interpreter, "cp") && isUnicodeDigits(string(r[2:])) {
			built := -1
			if string(r[2]) == major {
				n, err := strconv.Atoi(string(r[3:]))
				if err != nil {
					return false, errors.New("pypkg: int() of a wheel's interpreter")
				}
				built = n
			}
			if built >= 0 && built <= python[1] {
				return true, nil
			}
		}
	}
	return false, nil
}
