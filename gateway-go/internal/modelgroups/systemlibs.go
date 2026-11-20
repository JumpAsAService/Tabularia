package modelgroups

// A group whose image installs Debian libraries besides the fixed list every R image has (the
// recipe's `system_libraries`) says what they are on its Administration card. The recipe names
// Debian packages (it is the Dockerfile's input, and its hash decides when an image is built
// again, so it carries no display text); this is how a package is shown: the library's name and
// the version the image's base (Ubuntu 24.04) ships. A package not listed here is shown by its
// Debian name. The version a fit used is in the model card (`customer_lifetime.library`), read
// at run time; this is the version the image is meant to hold.

// SystemLibrary is a system library of a group's image as the card shows it.
type SystemLibrary struct {
	Package string `json:"package"`           // the Debian package of the recipe
	Name    string `json:"name"`              // «GSL»
	Version string `json:"version,omitempty"` // «2.7.1»; empty when not known
}

var systemLibraryInfo = map[string]SystemLibrary{
	"libgsl-dev": {Name: "GSL", Version: "2.7.1"}, // the GNU Scientific Library of Ubuntu 24.04 (2.7.1+dfsg-6ubuntu2)
}

// SystemLibraries are the group's system libraries as its card shows them, in the recipe's order;
// an empty (not nil) list for the groups that have none.
func (g Group) SystemLibraries() []SystemLibrary {
	out := []SystemLibrary{}
	for _, p := range g.Recipe.SystemLibraries {
		l, ok := systemLibraryInfo[p]
		if !ok {
			l.Name = p
		}
		l.Package = p
		out = append(out, l)
	}
	return out
}
