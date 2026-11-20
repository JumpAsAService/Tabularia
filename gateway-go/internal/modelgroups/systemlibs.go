package modelgroups

import "strings"

// A group whose image installs Debian libraries besides the fixed list every R image has (the
// recipe's `system_libraries`) says what they are on its Administration card. The recipe names
// Debian packages (it is the Dockerfile's input, and its hash decides when an image is built
// again, so it carries no display text). Two things come together here:
//
//   - the label, «GSL» for libgsl-dev: a name, not a datum, kept in the map below; a package it
//     does not list is shown by its Debian name;
//   - the version, which is never written here: the image's build records what apt installed
//     (scriptimage.SystemLibrariesFile, one `package@version` line each), the version's check adds
//     those lines to its lock, and the card reads the version from the lock of the version in use.
//     Before a build, or for a version built before the record existed, there is no version.

// SystemLibrary is a system library of a group's image as the card shows it.
type SystemLibrary struct {
	Package string `json:"package"`           // the Debian package of the recipe
	Name    string `json:"name"`              // «GSL»
	Version string `json:"version,omitempty"` // what the build installed; empty when no build has recorded it
}

var systemLibraryLabels = map[string]string{
	"libgsl-dev": "GSL", // the GNU Scientific Library
}

// SystemLibraries are the group's system libraries as its card shows them, in the recipe's order;
// an empty (not nil) list for the groups that have none. lock is the lock of the version in use
// (nil when there is none): its `package@version` lines give the versions installed.
func (g Group) SystemLibraries(lock *string) []SystemLibrary {
	installed := map[string]string{}
	if lock != nil {
		for _, line := range strings.Split(*lock, "\n") {
			if pkg, version, ok := strings.Cut(strings.TrimSpace(line), "@"); ok {
				installed[pkg] = version
			}
		}
	}
	out := []SystemLibrary{}
	for _, p := range g.Recipe.SystemLibraries {
		l := SystemLibrary{Package: p, Name: p, Version: installed[p]}
		if label, ok := systemLibraryLabels[p]; ok {
			l.Name = label
		}
		out = append(out, l)
	}
	return out
}
