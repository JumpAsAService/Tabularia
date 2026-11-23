package main

// `modelgroup pin <group>`: the sha256 of every package of a group's recipe, computed here and written into recipe.json
// (docs/specs/statistical-models.md, «The supply chain»). An R package: its source tarball, downloaded from CRAN (the current
// file or the archived one) and hashed; its DESCRIPTION says that every package it needs (Depends, Imports, LinkingTo) comes
// before it in the recipe, or is one of R's own. A Python package: every wheel of the version that a Linux build of the
// recipe's Python may install (x86_64 and aarch64), downloaded from PyPI and hashed, and compared with the digest PyPI
// publishes. The same files give the same recipe, byte for byte.

import (
	"archive/tar"
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
)

// rOwn are the packages every R image has without the recipe: R's base packages and the recommended ones rocker's images ship.
var rOwn = map[string]bool{}

func init() {
	for _, p := range strings.Fields("R base compiler datasets graphics grDevices grid methods parallel splines stats stats4 tcltk tools utils " +
		"KernSmooth MASS Matrix boot class cluster codetools foreign lattice mgcv nlme nnet rpart spatial survival") {
		rOwn[p] = true
	}
}

type pinner struct {
	client     *http.Client
	cran, pypi string
	log        io.Writer
}

func pinCommand(args []string, out io.Writer) error {
	fl := flag.NewFlagSet("pin", flag.ContinueOnError)
	cran := fl.String("cran", "https://cran.r-project.org", "the CRAN the R tarballs are hashed from")
	pypi := fl.String("pypi", "https://pypi.org", "the PyPI the wheels are hashed from (its JSON API and its files)")
	recipe := fl.String("recipe", "", "the recipe.json to rewrite (default internal/modelgroups/groups/<group>/recipe.json)")
	usage := errors.New("usage: modelgroup pin <group> [--recipe path] [--cran url] [--pypi url]")
	if err := fl.Parse(args); err != nil {
		return err
	}
	if fl.NArg() < 1 {
		return usage
	}
	group := fl.Arg(0)
	if err := fl.Parse(fl.Args()[1:]); err != nil || fl.NArg() != 0 {
		return usage
	}
	path := *recipe
	if path == "" {
		path = filepath.Join("internal", "modelgroups", "groups", group, "recipe.json")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	p := &pinner{client: &http.Client{Timeout: 20 * time.Minute}, cran: strings.TrimRight(*cran, "/"), pypi: strings.TrimRight(*pypi, "/"), log: out}
	pinned, err := p.pin(group, raw)
	if err != nil {
		return err
	}
	if bytes.Equal(pinned, raw) {
		_, err = fmt.Fprintf(out, "%s: the hashes are already the ones of the files\n", path)
		return err
	}
	if err := os.WriteFile(path, pinned, 0o644); err != nil {
		return err
	}
	_, err = fmt.Fprintf(out, "%s written\n", path)
	return err
}

// pin is the recipe raw with the hashes of its packages: its other keys as they were, in their order.
func (p *pinner) pin(group string, raw []byte) ([]byte, error) {
	var r modelgroups.Recipe
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&r); err != nil {
		return nil, fmt.Errorf("%s/recipe.json: %v", group, err)
	}
	hashes := map[string][]string{}
	switch r.Language {
	case "r":
		before := map[string]bool{}
		for _, pkg := range r.Packages {
			sum, err := p.rPackage(pkg, before)
			if err != nil {
				return nil, err
			}
			hashes[pkg] = []string{sum}
			before[strings.SplitN(pkg, "@", 2)[0]] = true
		}
	case "python":
		cp := "cp" + strings.ReplaceAll(r.Base, ".", "")
		for _, pkg := range r.Packages {
			sums, err := p.pyPackage(pkg, cp)
			if err != nil {
				return nil, err
			}
			hashes[pkg] = sums
		}
	default:
		return nil, fmt.Errorf("%s/recipe.json: language %q", group, r.Language)
	}
	r.Hashes = hashes
	if err := r.CheckHashes(group); err != nil {
		return nil, err
	}
	return rewrite(raw, r.Packages, hashes)
}

// download streams the file at address into a temporary file and hashes it on the way; the caller removes the file.
func (p *pinner) download(address string) (string, string, error) {
	resp, err := p.client.Get(address)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", "", fmt.Errorf("%s: %s", address, resp.Status)
	}
	f, err := os.CreateTemp("", "modelgroup-pin-*")
	if err != nil {
		return "", "", err
	}
	h := sha256.New()
	_, err = io.Copy(io.MultiWriter(f, h), resp.Body)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		_ = os.Remove(f.Name())
		return "", "", fmt.Errorf("%s: %v", address, err)
	}
	return f.Name(), hex.EncodeToString(h.Sum(nil)), nil
}

// rPackage is the sha256 of the source tarball of pkg (name@version), the current one or the archived one; its DESCRIPTION
// must be of that package and version, and every package it needs must be in before (or one of R's own).
func (p *pinner) rPackage(pkg string, before map[string]bool) (string, error) {
	name, version, ok := strings.Cut(pkg, "@")
	if !ok {
		return "", fmt.Errorf("«%s» is not name@version", pkg)
	}
	file := name + "_" + version + ".tar.gz"
	var tmp, sum string
	var err error
	for _, address := range []string{p.cran + "/src/contrib/" + file, p.cran + "/src/contrib/Archive/" + name + "/" + file} {
		if tmp, sum, err = p.download(address); err == nil {
			break
		}
	}
	if err != nil {
		return "", fmt.Errorf("%s: %v", pkg, err)
	}
	defer os.Remove(tmp)
	desc, err := description(tmp, name)
	if err != nil {
		return "", fmt.Errorf("%s: %v", file, err)
	}
	if desc["Package"] != name || desc["Version"] != version {
		return "", fmt.Errorf("%s is the package %s %s", file, desc["Package"], desc["Version"])
	}
	for _, field := range []string{"Depends", "Imports", "LinkingTo"} {
		for _, dep := range dependencies(desc[field]) {
			if !before[dep] && !rOwn[dep] {
				return "", fmt.Errorf("%s needs %s (%s), which is not before it in the recipe", pkg, dep, field)
			}
		}
	}
	fmt.Fprintf(p.log, "%s\t%s\n", pkg, sum)
	return sum, nil
}

// description is the DESCRIPTION of the package name in the tarball at path, its fields.
func description(path, name string) (map[string]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return nil, err
	}
	tr := tar.NewReader(gz)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil, errors.New("no DESCRIPTION")
		}
		if err != nil {
			return nil, err
		}
		if hdr.Name != name+"/DESCRIPTION" {
			continue
		}
		fields := map[string]string{}
		last := ""
		sc := bufio.NewScanner(io.LimitReader(tr, 1<<20))
		sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
		for sc.Scan() {
			line := sc.Text()
			if strings.HasPrefix(line, " ") || strings.HasPrefix(line, "\t") {
				if last != "" {
					fields[last] += " " + strings.TrimSpace(line)
				}
				continue
			}
			if k, v, ok := strings.Cut(line, ":"); ok {
				last = k
				fields[k] = strings.TrimSpace(v)
			}
		}
		return fields, sc.Err()
	}
}

// dependencies are the package names of a Depends, Imports or LinkingTo field, without their versions.
func dependencies(field string) []string {
	var out []string
	for _, part := range strings.Split(field, ",") {
		name, _, _ := strings.Cut(part, "(")
		if name = strings.TrimSpace(name); name != "" {
			out = append(out, name)
		}
	}
	return out
}

// pyRelease is what PyPI's JSON API says of a version: its files.
type pyRelease struct {
	URLs []struct {
		Filename    string            `json:"filename"`
		URL         string            `json:"url"`
		PackageType string            `json:"packagetype"`
		Digests     map[string]string `json:"digests"`
	} `json:"urls"`
}

// pyPackage is the sorted sha256 of the wheels of pkg (name==version) that a Linux build of the CPython cp (cp314) may install.
// Each is downloaded and hashed here, and must be the digest PyPI publishes. A version without a wheel for x86_64 is refused;
// without one for aarch64 it is said (that build would fail).
func (p *pinner) pyPackage(pkg, cp string) ([]string, error) {
	name, version, ok := strings.Cut(pkg, "==")
	if !ok {
		return nil, fmt.Errorf("«%s» is not name==version", pkg)
	}
	resp, err := p.client.Get(p.pypi + "/pypi/" + url.PathEscape(name) + "/" + url.PathEscape(version) + "/json")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: PyPI answers %s", pkg, resp.Status)
	}
	var rel pyRelease
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64<<20)).Decode(&rel); err != nil {
		return nil, fmt.Errorf("%s: %v", pkg, err)
	}
	var sums []string
	machines := map[string]bool{}
	for _, f := range rel.URLs {
		if f.PackageType != "bdist_wheel" {
			continue
		}
		fits := wheelMachines(f.Filename, cp)
		if len(fits) == 0 {
			continue
		}
		tmp, sum, err := p.download(f.URL)
		if err != nil {
			return nil, fmt.Errorf("%s: %v", pkg, err)
		}
		_ = os.Remove(tmp)
		if want := f.Digests["sha256"]; sum != want {
			return nil, fmt.Errorf("%s: %s has sha256 %s, PyPI says %s", pkg, f.Filename, sum, want)
		}
		for _, m := range fits {
			machines[m] = true
		}
		sums = append(sums, sum)
		fmt.Fprintf(p.log, "%s\t%s\t%s\n", pkg, sum, f.Filename)
	}
	if !machines["x86_64"] {
		return nil, fmt.Errorf("%s has no wheel for %s on Linux x86_64", pkg, cp)
	}
	if !machines["aarch64"] {
		fmt.Fprintf(p.log, "note: %s has no wheel for %s on Linux aarch64: an arm64 build of the group fails\n", pkg, cp)
	}
	sort.Strings(sums)
	return sums, nil
}

// wheelMachines are the machines (x86_64, aarch64) of a glibc Linux whose CPython cp (cp314) may install the wheel file;
// none when it is for another Python, a free-threaded one, or another system.
func wheelMachines(file, cp string) []string {
	parts := strings.Split(strings.TrimSuffix(file, ".whl"), "-")
	if !strings.HasSuffix(file, ".whl") || len(parts) < 5 {
		return nil
	}
	pys, abis, plats := strings.Split(parts[len(parts)-3], "."), strings.Split(parts[len(parts)-2], "."), strings.Split(parts[len(parts)-1], ".")
	minor, _ := strconv.Atoi(strings.TrimPrefix(cp, "cp3"))
	python := false
	for _, py := range pys {
		for _, abi := range abis {
			switch {
			case abi == "none" && (py == "py3" || py == cp || py == "py3"+strconv.Itoa(minor)):
				python = true
			case abi == cp && py == cp:
				python = true
			case abi == "abi3" && strings.HasPrefix(py, "cp3"):
				if m, err := strconv.Atoi(strings.TrimPrefix(py, "cp3")); err == nil && m <= minor {
					python = true
				}
			}
		}
	}
	if !python {
		return nil
	}
	seen := map[string]bool{}
	var out []string
	for _, plat := range plats {
		m := ""
		switch {
		case plat == "any":
			for _, both := range []string{"x86_64", "aarch64"} {
				if !seen[both] {
					seen[both] = true
					out = append(out, both)
				}
			}
			continue
		case strings.HasPrefix(plat, "manylinux") && strings.HasSuffix(plat, "_x86_64"):
			m = "x86_64"
		case strings.HasPrefix(plat, "manylinux") && strings.HasSuffix(plat, "_aarch64"):
			m = "aarch64"
		}
		if m != "" && !seen[m] {
			seen[m] = true
			out = append(out, m)
		}
	}
	return out
}

// rewrite is the recipe raw with `hashes` set (after `packages` when it had none), its other keys in their order and indented
// as encoding/json indents them; `hashes` holds one line a package, in the recipe's order.
func rewrite(raw []byte, packages []string, hashes map[string][]string) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return nil, errors.New("the recipe is not a JSON object")
	}
	var keys []string
	values := map[string]json.RawMessage{}
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key := t.(string)
		var v json.RawMessage
		if err := dec.Decode(&v); err != nil {
			return nil, err
		}
		if _, dup := values[key]; !dup {
			keys = append(keys, key)
		}
		values[key] = v
	}
	if _, ok := values["hashes"]; !ok {
		at := len(keys)
		for i, k := range keys {
			if k == "packages" {
				at = i + 1
			}
		}
		keys = append(keys[:at], append([]string{"hashes"}, keys[at:]...)...)
	}
	var b bytes.Buffer
	b.WriteString("{\n")
	for i, key := range keys {
		k, _ := json.Marshal(key)
		b.WriteString("  " + string(k) + ": ")
		if key == "hashes" {
			b.WriteString("{\n")
			for j, pkg := range packages {
				name, _ := json.Marshal(pkg)
				sums, _ := json.Marshal(hashes[pkg])
				b.WriteString("    " + string(name) + ": " + strings.ReplaceAll(string(sums), `","`, `", "`))
				if j < len(packages)-1 {
					b.WriteString(",")
				}
				b.WriteString("\n")
			}
			b.WriteString("  }")
		} else {
			var compact, indented bytes.Buffer
			if err := json.Compact(&compact, values[key]); err != nil {
				return nil, err
			}
			if err := json.Indent(&indented, compact.Bytes(), "  ", "  "); err != nil {
				return nil, err
			}
			b.Write(indented.Bytes())
		}
		if i < len(keys)-1 {
			b.WriteString(",")
		}
		b.WriteString("\n")
	}
	b.WriteString("}\n")
	return b.Bytes(), nil
}
